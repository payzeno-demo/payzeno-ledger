"""`GET /internal/v1/balances/{merchantId}` without `?currency=` — RFC platform-v2 §7.

Balances are per currency: a EUR payment for a USD merchant lands in the EUR balance and is
never converted at capture. Asked without a currency, the route answers every balance the
merchant holds, `{"data": [Balance, ...]}`, one per currency, each exactly the `Balance`
contract the single-currency form returns.

The service is the real `BalanceService` over an in-memory cache, so the path under test
runs from the route through `list_balances` to the shaping in `_balance`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.routers.balances import get_balance
from app.api.schemas import Balance, BalanceListResponse
from app.services.balances import BalanceService
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
MERCHANT = "mer_multicurrency"


class Row:
    def __init__(self, currency: str, available: int, pending: int = 0, livemode: bool = True):
        self.merchant_id = MERCHANT
        self.currency = currency
        self.livemode = livemode
        self.available_minor = available
        self.pending_minor = pending
        self.reserved_minor = 0
        self.disputed_minor = 0
        self.computed_at = NOW


class Cache:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.asked: list[dict[str, Any]] = []

    async def get(self, session: Any, *, merchant_id: str, currency: str, livemode: bool) -> Any:
        return next(
            (
                r
                for r in self.rows
                if (r.merchant_id, r.currency, r.livemode) == (merchant_id, currency, livemode)
            ),
            None,
        )

    async def list_for_merchant(
        self, session: Any, *, merchant_id: str, livemode: bool
    ) -> list[Row]:
        self.asked.append({"merchant_id": merchant_id, "livemode": livemode})
        return sorted(
            (r for r in self.rows if r.merchant_id == merchant_id and r.livemode == livemode),
            key=lambda r: r.currency,
        )


class Sessions:
    @asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        yield object()


def _service(rows: list[Row]) -> tuple[BalanceService, Cache]:
    cache = Cache(rows)
    service = BalanceService(
        sessions=Sessions(),
        balances=cache,
        entries=None,
        accounts=None,
        clock=FrozenClock(NOW),
    )
    return service, cache


async def test_without_a_currency_every_balance_comes_back_one_per_currency() -> None:
    service, _ = _service([Row("USD", 418_000, 2_500), Row("EUR", 9_710)])

    body = await get_balance(service, MERCHANT, None)

    BalanceListResponse.model_validate(body)
    assert [row["currency"] for row in body["data"]] == ["EUR", "USD"]
    eur = body["data"][0]
    assert eur["merchant_id"] == MERCHANT
    assert eur["available_minor"] == 9_710
    assert eur["pending_minor"] == 0
    assert eur["negative_balance_minor"] == 0
    assert eur["object"] == "balance"


async def test_each_entry_is_exactly_the_single_currency_shape() -> None:
    service, _ = _service([Row("EUR", 9_710)])

    listed = (await get_balance(service, MERCHANT, None))["data"][0]
    single = await get_balance(service, MERCHANT, "EUR")

    Balance.model_validate(listed)
    assert listed == single
    assert set(listed) == {
        "object",
        "merchant_id",
        "currency",
        "available_minor",
        "pending_minor",
        "reserved_minor",
        "disputed_minor",
        "negative_balance_minor",
        "buckets",
        "as_of",
    }


async def test_a_merchant_with_no_postings_gets_an_empty_list() -> None:
    service, _ = _service([])

    body = await get_balance(service, MERCHANT, None)

    assert body == {"data": []}


async def test_livemode_selects_which_balances_are_listed() -> None:
    service, cache = _service([Row("USD", 100), Row("USD", 7, livemode=False)])

    body = await get_balance(service, MERCHANT, None, None, False)

    assert cache.asked == [{"merchant_id": MERCHANT, "livemode": False}]
    assert [row["available_minor"] for row in body["data"]] == [7]


async def test_a_debit_balance_is_reported_as_owed_not_as_negative_available() -> None:
    service, _ = _service([Row("GBP", -1_500)])

    gbp = (await get_balance(service, MERCHANT, None))["data"][0]

    assert gbp["available_minor"] == 0
    assert gbp["negative_balance_minor"] == 1_500
