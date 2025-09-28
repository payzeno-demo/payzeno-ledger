"""`MerchantProjectionRepository` — app/repositories/projections.py.

Every write is a conditional upsert guarded on `source_occurred_at` (migration 0031). The
question I asked when that migration went in was "why not just trust the event id?" and the
answer is that `source_event_id` is a ULID of the EVENT, not a monotonic token for the
merchant — two events emitted 30ms apart by different pods sort by their own creation, not
by the order the merchant actually changed.

Without the guard, a redelivered `merchant.status_changed` silently un-restricts a suspended
merchant, and a redelivered `merchant.updated` re-enables `capture_at_settlement` in our view
of the world. The second one is the interesting failure: `SettlementPoster` reads that flag
off the CHARGE, so a stale merchant row cannot double-capture anybody — but
`settlement_tolerance_minor` comes off the merchant, and a stale tolerance decides whether a
variance is posted or quarantined.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.repositories.projections import MerchantProjectionRepository
from tests.factories import make_merchant_projection

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

T0 = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo() -> MerchantProjectionRepository:
    return MerchantProjectionRepository()


async def test_insert_then_read(session, repo: MerchantProjectionRepository) -> None:
    await repo.upsert_if_newer(
        session, make_merchant_projection(merchant_id="mer_mp_1", source_occurred_at=T0)
    )
    await session.flush()

    merchant = await repo.get_or_raise(session, "mer_mp_1")
    assert merchant.merchant_id == "mer_mp_1"
    assert merchant.settlement_tolerance_minor == 100


async def test_a_newer_event_wins(session, repo: MerchantProjectionRepository) -> None:
    await repo.upsert_if_newer(
        session,
        make_merchant_projection(merchant_id="mer_mp_2", status="active", source_occurred_at=T0),
    )
    await session.flush()

    await repo.upsert_if_newer(
        session,
        make_merchant_projection(
            merchant_id="mer_mp_2", status="restricted", source_occurred_at=T0 + timedelta(seconds=30)
        ),
    )
    await session.flush()

    assert (await repo.get_or_raise(session, "mer_mp_2")).status == "restricted"


async def test_an_older_event_is_ignored(session, repo: MerchantProjectionRepository) -> None:
    await repo.upsert_if_newer(
        session,
        make_merchant_projection(
            merchant_id="mer_mp_3", status="suspended", source_occurred_at=T0
        ),
    )
    await session.flush()

    await repo.upsert_if_newer(
        session,
        make_merchant_projection(
            merchant_id="mer_mp_3", status="active", source_occurred_at=T0 - timedelta(minutes=10)
        ),
    )
    await session.flush()

    # The whole point. A late-arriving "active" must not un-suspend a suspended merchant.
    assert (await repo.get_or_raise(session, "mer_mp_3")).status == "suspended"


async def test_an_event_with_an_identical_timestamp_does_not_apply(
    session, repo: MerchantProjectionRepository
) -> None:
    # The predicate is strictly `<`, so a redelivery of the exact same event is a no-op
    # rather than a rewrite. Redelivery is normal — at-least-once, always.
    await repo.upsert_if_newer(
        session,
        make_merchant_projection(merchant_id="mer_mp_4", risk_tier="standard", source_occurred_at=T0),
    )
    await session.flush()

    await repo.upsert_if_newer(
        session,
        make_merchant_projection(merchant_id="mer_mp_4", risk_tier="high", source_occurred_at=T0),
    )
    await session.flush()

    assert (await repo.get_or_raise(session, "mer_mp_4")).risk_tier == "standard"


async def test_out_of_order_capture_at_settlement_cannot_be_re_enabled(
    session, repo: MerchantProjectionRepository
) -> None:
    await repo.upsert_if_newer(
        session,
        make_merchant_projection(
            merchant_id="mer_mp_5", capture_at_settlement=True, source_occurred_at=T0
        ),
    )
    await session.flush()
    await repo.upsert_if_newer(
        session,
        make_merchant_projection(
            merchant_id="mer_mp_5",
            capture_at_settlement=False,
            source_occurred_at=T0 + timedelta(minutes=1),
        ),
    )
    await session.flush()

    stale_redelivery = make_merchant_projection(
        merchant_id="mer_mp_5", capture_at_settlement=True, source_occurred_at=T0
    )
    await repo.upsert_if_newer(session, stale_redelivery)
    await session.flush()

    assert (await repo.get_or_raise(session, "mer_mp_5")).capture_at_settlement is False


async def test_status_only_update_does_not_clobber_pricing(
    session, repo: MerchantProjectionRepository
) -> None:
    """`merchant.status_changed` carries the status and nothing else.

    Upserting a partially populated row over a full one would zero `platform_fee_bps` and
    every capture after it would book no revenue at all.
    """
    await repo.upsert_if_newer(
        session,
        make_merchant_projection(
            merchant_id="mer_mp_6",
            platform_fee_bps=290,
            platform_fee_fixed_minor=30,
            source_occurred_at=T0,
        ),
    )
    await session.flush()

    await repo.apply_status_change(
        session,
        merchant_id="mer_mp_6",
        status="restricted",
        source_event_id="evt_status_1",
        source_occurred_at=T0 + timedelta(minutes=5),
    )
    await session.flush()

    merchant = await repo.get_or_raise(session, "mer_mp_6")
    assert merchant.status == "restricted"
    assert merchant.platform_fee_bps == 290
    assert merchant.platform_fee_fixed_minor == 30


async def test_apply_status_change_is_also_ordering_guarded(
    session, repo: MerchantProjectionRepository
) -> None:
    await repo.upsert_if_newer(
        session,
        make_merchant_projection(merchant_id="mer_mp_7", status="suspended", source_occurred_at=T0),
    )
    await session.flush()

    await repo.apply_status_change(
        session,
        merchant_id="mer_mp_7",
        status="active",
        source_event_id="evt_status_2",
        source_occurred_at=T0 - timedelta(days=1),
    )
    await session.flush()

    assert (await repo.get_or_raise(session, "mer_mp_7")).status == "suspended"


async def test_settlement_tolerance_defaults_to_one_unit(
    session, repo: MerchantProjectionRepository
) -> None:
    # `SettlementPoster` compares abs(variance_minor) against this before anything posts.
    # A NULL here would make that comparison a TypeError on the money path.
    await repo.upsert_if_newer(
        session, make_merchant_projection(merchant_id="mer_mp_8", source_occurred_at=T0)
    )
    await session.flush()

    merchant = await repo.get_or_raise(session, "mer_mp_8")
    assert merchant.settlement_tolerance_minor is not None
    assert merchant.settlement_tolerance_minor >= 0
