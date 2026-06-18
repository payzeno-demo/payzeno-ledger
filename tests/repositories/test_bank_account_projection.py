"""`BankAccountProjectionRepository` — app/repositories/projections.py.

`payout.bank_account_id` points at a row payzeno-api owns and the ledger has no route to.
This projection is how the account number token reaches the rail: `PayoutInitiator.initiate`
takes the projection explicitly, and `CreatePayoutRequest.bank_account_id` is optional, so
the ledger has to be able to find the merchant's DEFAULT account for a currency on its own.
That is what `pix_bank_account_projection_default` is for.

Nothing here stores a bank account number. `account_number_token` plus the last four is the
whole of it — arc PCI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.errors import BankAccountProjectionNotFoundError
from app.models.projections import BankAccountProjection
from app.repositories.projections import BankAccountProjectionRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

T0 = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo() -> BankAccountProjectionRepository:
    return BankAccountProjectionRepository()


def _bank(bank_account_id: str, **kw: object) -> BankAccountProjection:
    defaults: dict[str, object] = {
        "bank_account_id": bank_account_id,
        "merchant_id": "mer_ba_1",
        "currency": "USD",
        "country": "US",
        "scheme": "aba",
        "account_number_token": f"tok_{bank_account_id}",
        "routing_last_four": "0021",
        "status": "verified",
        "is_default": True,
        "livemode": True,
        "source_event_id": "evt_1",
        "source_occurred_at": T0,
    }
    defaults.update(kw)
    return BankAccountProjection(**defaults)  # type: ignore[arg-type]


async def test_upsert_and_get_default(session, repo: BankAccountProjectionRepository) -> None:
    await repo.upsert_if_newer(session, _bank("ba_1"))
    await session.flush()

    found = await repo.get_default(session, merchant_id="mer_ba_1", currency="USD", livemode=True)
    assert found.bank_account_id == "ba_1"
    assert found.account_number_token == "tok_ba_1"


async def test_get_default_raises_when_there_is_none(
    session, repo: BankAccountProjectionRepository
) -> None:
    """The payout path's own 4xx.

    A merchant with no verified default account for that currency cannot be paid out, and
    the failure has to name itself — "payout failed" with no reason is a support ticket.
    """
    with pytest.raises(BankAccountProjectionNotFoundError):
        await repo.get_default(session, merchant_id="mer_ba_nobody", currency="USD", livemode=True)


async def test_only_one_default_per_merchant_currency_livemode(
    session, repo: BankAccountProjectionRepository
) -> None:
    await repo.upsert_if_newer(session, _bank("ba_2", is_default=True))
    await session.flush()

    session.add(_bank("ba_3", is_default=True))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_non_default_accounts_may_coexist(
    session, repo: BankAccountProjectionRepository
) -> None:
    # The partial index only constrains rows where is_default is true, so a merchant may
    # keep any number of secondary accounts on file.
    await repo.upsert_if_newer(session, _bank("ba_4", is_default=True))
    await repo.upsert_if_newer(session, _bank("ba_5", is_default=False))
    await repo.upsert_if_newer(session, _bank("ba_6", is_default=False))
    await session.flush()

    all_for_merchant = await repo.list_for_merchant(
        session, merchant_id="mer_ba_1", currency="USD"
    )
    assert len(all_for_merchant) == 3


async def test_currencies_have_independent_defaults(
    session, repo: BankAccountProjectionRepository
) -> None:
    await repo.upsert_if_newer(session, _bank("ba_7", currency="USD"))
    await repo.upsert_if_newer(
        session, _bank("ba_8", currency="EUR", scheme="iban", iban_last_four="4412", routing_last_four=None)
    )
    await session.flush()

    usd = await repo.get_default(session, merchant_id="mer_ba_1", currency="USD", livemode=True)
    eur = await repo.get_default(session, merchant_id="mer_ba_1", currency="EUR", livemode=True)

    assert usd.scheme == "aba"
    assert eur.scheme == "iban"


async def test_upsert_ignores_an_older_event(session, repo: BankAccountProjectionRepository) -> None:
    await repo.upsert_if_newer(session, _bank("ba_9", status="verified", source_occurred_at=T0))
    await session.flush()

    await repo.upsert_if_newer(
        session, _bank("ba_9", status="pending", source_occurred_at=T0 - timedelta(hours=2))
    )
    await session.flush()

    found = await repo.get(session, "ba_9")
    assert found is not None
    assert found.status == "verified"


async def test_upsert_applies_a_newer_event(session, repo: BankAccountProjectionRepository) -> None:
    await repo.upsert_if_newer(session, _bank("ba_10", status="verified", source_occurred_at=T0))
    await session.flush()

    await repo.upsert_if_newer(
        session, _bank("ba_10", status="errored", source_occurred_at=T0 + timedelta(minutes=1))
    )
    await session.flush()

    found = await repo.get(session, "ba_10")
    assert found is not None
    assert found.status == "errored"


async def test_the_projection_stores_a_token_and_never_an_account_number(
    session, repo: BankAccountProjectionRepository
) -> None:
    await repo.upsert_if_newer(session, _bank("ba_11"))
    await session.flush()

    projection = await repo.get(session, "ba_11")
    assert projection is not None
    columns = {c.name for c in projection.__table__.columns}
    assert "account_number_token" in columns
    assert not columns & {"account_number", "iban", "routing_number", "sort_code"}
    assert len(projection.routing_last_four or "") <= 4
