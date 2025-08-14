"""`PayoutRepository` — app/repositories/payout.py.

Inherited from Maya when she handed over. Two constraints here carry real weight:

`pix_payout_in_flight (merchant_id, currency, livemode) unique where status in
('scheduled','in_transit')` — unique since migration 0033. `compute_available` subtracts
in-flight payouts by reading rows a concurrent uncommitted transaction has not written yet,
which is the identical check-then-act shape as PAY-2041, on the path that moves money to a
bank account. `PayoutService.create_payout` takes the merchant/currency advisory lock first;
this index is the database's last word if that is ever bypassed.

`chk_payout_reversal_present` — `failed` and `returned` are terminal and the payout already
debited `merchant_payable`. Without a reversal transaction the merchant's money is simply
gone. That is invariant 7.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.payout import Payout
from app.repositories.payout import PayoutRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)


@pytest.fixture
def repo() -> PayoutRepository:
    return PayoutRepository()


def _payout(payout_id: str, **kw: object) -> Payout:
    defaults: dict[str, object] = {
        "id": payout_id,
        "merchant_id": "mer_po_1",
        "bank_account_id": "ba_1",
        "amount_minor": 418_000,
        "currency": "USD",
        "status": "scheduled",
        "method": "standard_ach",
        "available_on": date(2026, 4, 20),
        "statement_descriptor": "PAYZENO PAYOUT",
        "livemode": True,
    }
    defaults.update(kw)
    return Payout(**defaults)  # type: ignore[arg-type]


async def test_add_and_get(session, repo: PayoutRepository) -> None:
    await repo.add(session, _payout("po_1"))
    await session.flush()

    assert (await repo.get_or_raise(session, "po_1")).amount_minor == 418_000


async def test_only_one_in_flight_payout_per_merchant_currency(
    session, repo: PayoutRepository
) -> None:
    await repo.add(session, _payout("po_2", status="scheduled"))
    await session.flush()

    await repo.add(session, _payout("po_3", status="in_transit"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_terminal_payout_frees_the_in_flight_slot(session, repo: PayoutRepository) -> None:
    await repo.add(session, _payout("po_4", status="scheduled"))
    await session.flush()

    await repo.mark_paid(session, "po_4", paid_at=NOW, bank_reference="ACH-0001")
    await session.flush()

    await repo.add(session, _payout("po_5", status="scheduled"))
    await session.flush()  # allowed — the first one is `paid`, not in flight


async def test_livemode_is_part_of_the_in_flight_key(session, repo: PayoutRepository) -> None:
    # Test-mode payouts must not block a live payout for the same merchant.
    await repo.add(session, _payout("po_6", livemode=True))
    await repo.add(session, _payout("po_7", livemode=False))
    await session.flush()


async def test_failed_without_a_reversal_violates_the_check(
    session, repo: PayoutRepository
) -> None:
    await repo.add(session, _payout("po_8", status="scheduled"))
    await session.flush()

    payout = await repo.get_or_raise(session, "po_8")
    payout.status = "failed"
    payout.failure_code = "account_closed"
    payout.failure_message = "R02"

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_failed_with_a_reversal_is_accepted(session, repo: PayoutRepository) -> None:
    await repo.add(session, _payout("po_9", status="scheduled"))
    await session.flush()

    updated = await repo.mark_failed(
        session,
        "po_9",
        failure_code="account_closed",
        failure_message="R02 account closed",
        reversal_transaction_id="txn_reversal_1",
        at=NOW,
    )

    assert updated.status == "failed"
    assert updated.reversal_transaction_id == "txn_reversal_1"
    assert updated.failed_at is not None


async def test_returned_also_requires_a_reversal(session, repo: PayoutRepository) -> None:
    # ACH returns arrive up to 60 days after `paid`. The reversal is not optional just
    # because the money already left.
    await repo.add(session, _payout("po_10", status="scheduled"))
    await session.flush()
    await repo.mark_paid(session, "po_10", paid_at=NOW, bank_reference="ACH-0002")
    await session.flush()

    returned = await repo.mark_returned(
        session, "po_10", reversal_transaction_id="txn_reversal_2", at=NOW
    )
    assert returned.status == "returned"
    assert returned.reversal_transaction_id == "txn_reversal_2"


async def test_sum_in_flight_only_counts_scheduled_and_in_transit(
    session, repo: PayoutRepository
) -> None:
    await repo.add(session, _payout("po_11", status="scheduled", amount_minor=100))
    await repo.add(session, _payout("po_12", merchant_id="mer_po_2", status="paid", amount_minor=900))
    await repo.add(
        session, _payout("po_13", merchant_id="mer_po_3", status="canceled", amount_minor=500)
    )
    await session.flush()

    total = await repo.sum_in_flight(session, merchant_id="mer_po_1", currency="USD", livemode=True)
    assert total == 100


async def test_sum_in_flight_is_zero_when_nothing_is_pending(
    session, repo: PayoutRepository
) -> None:
    assert (
        await repo.sum_in_flight(
            session, merchant_id="mer_po_quiet", currency="USD", livemode=True
        )
        == 0
    )


async def test_list_due_for_initiation(session, repo: PayoutRepository) -> None:
    await repo.add(session, _payout("po_14", available_on=date(2026, 4, 16)))
    await repo.add(
        session,
        _payout("po_15", merchant_id="mer_po_4", available_on=date(2026, 5, 1)),
    )
    await session.flush()

    due = await repo.list_due(session, on=date(2026, 4, 16))

    assert [p.id for p in due] == ["po_14"]


async def test_cancel_is_only_valid_from_scheduled(session, repo: PayoutRepository) -> None:
    await repo.add(session, _payout("po_16", status="in_transit"))
    await session.flush()

    with pytest.raises(ValueError, match="scheduled"):
        await repo.mark_canceled(session, "po_16", at=NOW)
