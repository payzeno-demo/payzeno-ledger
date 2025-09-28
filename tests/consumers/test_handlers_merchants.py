"""Merchant event handlers — app/consumers/handlers/merchants.py.

Four handlers, four projection writes, and one field that matters more than all the rest:
`merchant_projection.capture_at_settlement`. `handle_merchant_updated` is the only
mechanism by which it ever becomes true in the ledger, and it is what took PAY-2041 from
1,847 duplicate ledger rows to 218 double-charged cardholders. It gets a `logger.warning`
on every change and it gets tests here.

`handle_merchant_created` also bootstraps the merchant's account set. It does that through
`AccountResolver`, which is the single writer of `account` — the HTTP bootstrap route and
lazy posting-time resolution go through the same method. One writer, three callers, no
third mechanism.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.consumers.handlers.merchants import (
    handle_bank_account_verified,
    handle_merchant_created,
    handle_merchant_status_changed,
    handle_merchant_updated,
)
from app.repositories.projections import (
    BankAccountProjectionRepository,
    MerchantProjectionRepository,
)
from app.services.accounts import AccountResolver
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 13, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=3)


class UpsertingMerchants(MerchantProjectionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, Any] = {}
        self.rejected: list[str] = []
        self.status_updates: list[tuple[str, str]] = []

    async def get(self, session: Any, entity_id: str) -> Any | None:
        return self.rows.get(entity_id)

    async def upsert_if_newer(self, session: Any, projection: Any) -> bool:
        existing = self.rows.get(projection.merchant_id)
        if existing is not None and existing.source_occurred_at >= projection.source_occurred_at:
            self.rejected.append(projection.merchant_id)
            return False
        self.rows[projection.merchant_id] = projection
        return True

    async def update_status_if_newer(
        self, session: Any, *, merchant_id: str, status: str, occurred_at: datetime, **kwargs: Any
    ) -> Any | None:
        row = self.rows.get(merchant_id)
        if row is None or row.source_occurred_at > occurred_at:
            return None
        row.status = status
        row.source_occurred_at = occurred_at
        self.status_updates.append((merchant_id, status))
        return row


class UpsertingBanks(BankAccountProjectionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, Any] = {}
        self.cleared: list[dict[str, Any]] = []
        self.rejected: list[str] = []

    async def upsert_if_newer(self, session: Any, projection: Any) -> bool:
        merchants=merchants,
        event_id="evt_u1",
        livemode=True,
    )

    row = merchants.rows["mer_c1"]
    assert row.country == "GB"
    assert row.default_currency == "GBP"
    assert row.display_name == "Loomcraft Ltd"


async def test_an_update_before_the_create_is_dropped_not_invented() -> None:
    """Out-of-order delivery. A projection built from an update alone has no country.

    Dropping it is right: `merchant.created` will arrive and rebuild it properly, and
    until it does the ledger has no charges for this merchant anyway.
    """
    merchants = UpsertingMerchants()

    await handle_merchant_updated(
        object(),
        _updated_payload(),
        occurred_at=NOW,
        livemode=True,
    )

    assert merchants.rows == {}


# --------------------------------------------------------------------------------------
# merchant.status_changed
# --------------------------------------------------------------------------------------


async def test_status_change_applies_to_the_projection() -> None:
    merchants = UpsertingMerchants()
    await handle_merchant_created(
        object(),
        _created_payload(),
        event_id="evt_b1",
        occurred_at=NOW,
        event_id="evt_b1",
        clock=FrozenClock(NOW),
        livemode=True,
    )

    assert banks.cleared[0]["keep"] == "ba_2"
    assert banks.cleared[0]["merchant_id"] == "mer_c1"


async def test_a_non_default_account_does_not_clear_anything() -> None:
    banks = UpsertingBanks()

    await handle_bank_account_verified(
        object(),
        _bank_payload(bank_account_id="ba_3", is_default=False),
        occurred_at=NOW,
    )

    assert banks.rows["ba_1"].account_number_token == "tok_bank_1"
    assert banks.rejected == ["ba_1"]
