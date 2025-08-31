"""`SettlementChargeRepository` — app/repositories/settlement_charge.py.

This is a PROJECTION of payzeno-api's `charge`, not our own record. Everything on it was
denormalised at authorisation time so that a later merchant change cannot retroactively
alter an in-flight settlement — and that reasoning has far higher stakes for
`capture_at_settlement` than for `reserve_bps`, because flipping the merchant flag
mid-flight would otherwise decide whether an already-authorised charge gets a second
cardholder capture.

`SettlementPoster` therefore reads `charge.capture_at_settlement`, never
`merchant.capture_at_settlement`. There is a test below that says so out loud.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.errors import ChargeProjectionNotFoundError
from app.repositories.settlement_charge import SettlementChargeRepository
from tests.factories import make_charge_projection

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

T0 = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo() -> SettlementChargeRepository:
    return SettlementChargeRepository()


async def test_get_or_raise_returns_the_projection(
    session, repo: SettlementChargeRepository
) -> None:
    await repo.upsert_if_newer(
        session, make_charge_projection(charge_id="ch_sc_1", source_occurred_at=T0)
    )
    await session.flush()

    charge = await repo.get_or_raise(session, "ch_sc_1")
    assert charge.charge_id == "ch_sc_1"


async def test_get_or_raise_raises_the_projection_specific_error(
    session, repo: SettlementChargeRepository
) -> None:
    """Not a generic NotFoundError.

    `SettlementPoster` guards `item.charge_id is None` BEFORE calling this, so a null charge
    id raises OrphanedItemError. Reaching this error instead means the item was matched to a
    charge we have never seen, which is a projection lag problem, not an orphan.
    """
    with pytest.raises(ChargeProjectionNotFoundError):
        await repo.get_or_raise(session, "ch_never_seen")


async def test_find_by_processor_reference_is_the_primary_match_key(
    session, repo: SettlementChargeRepository
) -> None:
    await repo.upsert_if_newer(
        session,
        make_charge_projection(
            charge_id="ch_sc_2",
            acquirer="worldflow",
            processor_reference="WF-8831-0042",
            source_occurred_at=T0,
        ),
    )
    await session.flush()

    found = await repo.find_by_processor_reference(
        session, acquirer="worldflow", processor_reference="WF-8831-0042"
    )

    assert found is not None
    assert found.charge_id == "ch_sc_2"


async def test_processor_reference_lookup_is_scoped_to_the_acquirer(
    session, repo: SettlementChargeRepository
) -> None:
    # Two acquirers can and do mint colliding reference strings. `ExactReferenceMatch` would
    # otherwise settle a Worldflow line against a Nordpay charge.
    await repo.upsert_if_newer(
        session,
        make_charge_projection(
            charge_id="ch_sc_3", acquirer="worldflow", processor_reference="REF-1", source_occurred_at=T0
        ),
    )
    await session.flush()

    assert (
        await repo.find_by_processor_reference(
            session, acquirer="nordpay", processor_reference="REF-1"
        )
        is None
    )


async def test_network_transaction_id_is_unique_per_acquirer(
    session, repo: SettlementChargeRepository
) -> None:
    await repo.upsert_if_newer(
        session,
        make_charge_projection(
            charge_id="ch_sc_4", acquirer="worldflow", network_transaction_id="NTID-1", source_occurred_at=T0
        ),
    )
    await session.flush()

    session.add(
        make_charge_projection(
            charge_id="ch_sc_5", acquirer="worldflow", network_transaction_id="NTID-1", source_occurred_at=T0
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_upsert_if_newer_applies_a_later_event(
    session, repo: SettlementChargeRepository
) -> None:
    await repo.upsert_if_newer(
        session, make_charge_projection(charge_id="ch_sc_6", amount_minor=10_000, source_occurred_at=T0)
    )
    await session.flush()

    await repo.upsert_if_newer(
        session,
        make_charge_projection(
            charge_id="ch_sc_6", amount_minor=12_000, source_occurred_at=T0 + timedelta(minutes=5)
        ),
    )
    await session.flush()

    assert (await repo.get_or_raise(session, "ch_sc_6")).amount_minor == 12_000


async def test_upsert_if_newer_ignores_an_older_event(
    session, repo: SettlementChargeRepository
) -> None:
    """Ordering is not guaranteed on the bus.

    `source_event_id` is a ULID of the EVENT, not a monotonic token for the entity, so the
    guard has to be on `source_occurred_at`. Without it a redelivered
    `payment.authorized` can re-enable `capture_at_settlement` in our view of a charge that
    has since been corrected.
    """
    await repo.upsert_if_newer(
        session,
        make_charge_projection(
            charge_id="ch_sc_7", capture_at_settlement=False, source_occurred_at=T0
        ),
    )
    await session.flush()

    await repo.upsert_if_newer(
        session,
        make_charge_projection(
            charge_id="ch_sc_7",
            capture_at_settlement=True,
            source_occurred_at=T0 - timedelta(hours=1),
        ),
    )
    await session.flush()

    assert (await repo.get_or_raise(session, "ch_sc_7")).capture_at_settlement is False


async def test_capture_at_settlement_is_carried_on_the_charge(
    session, repo: SettlementChargeRepository
) -> None:
    # The 11 travel/lodging merchants from PAY-1652 are visible here, per charge. Reading
    # the flag off `merchant_projection` instead would mean a merchant toggling it after
    # authorisation decides whether an in-flight charge gets a second capture.
    await repo.upsert_if_newer(
        session,
        make_charge_projection(
            charge_id="ch_sc_8", capture_at_settlement=True, source_occurred_at=T0
        ),
    )
    await session.flush()

    charge = await repo.get_or_raise(session, "ch_sc_8")
    assert charge.capture_at_settlement is True
    assert hasattr(charge, "reserve_bps")
    assert hasattr(charge, "platform_fee_bps")
    assert hasattr(charge, "platform_fee_fixed_minor")


async def test_the_projection_never_stores_a_pan(session, repo: SettlementChargeRepository) -> None:
    # arc PCI arrives in this repo as review pushback rather than as work, but the schema
    # assertion is cheap and it is the one that would actually catch a regression.
    await repo.upsert_if_newer(
        session, make_charge_projection(charge_id="ch_sc_9", source_occurred_at=T0)
    )
    await session.flush()

    charge = await repo.get_or_raise(session, "ch_sc_9")
    columns = {c.name for c in charge.__table__.columns}
    assert not columns & {"pan", "card_number", "primary_account_number"}
