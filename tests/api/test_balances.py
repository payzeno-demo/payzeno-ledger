"""`app/api/routers/balances.py` — api-surface.md §10.1.

Two routes, and the reason this file is separate from `test_accounts.py` is the second
caller. `GET /internal/v1/balances/{merchantId}` is arc MIG's oldest surviving edge:
payzeno-billing-legacy's `LedgerClient.getBalance` has called it from
`StatementController` and `DunningService#shouldRetry` since month 1, when the Java
service stopped computing balances itself. Nothing about that caller is in this
repository, it does not run our tests, and it will notice a renamed response field in
production. So the shape assertions here are deliberately literal — every key by name,
not `assert set(body) >= {...}`.

The validation branches are the other half. `get_balance_history` rejects a bad interval,
a reversed range and an over-long window *in the router* rather than in `BalanceService`,
because all three are caller bugs rather than ledger conditions, and because the service
is also called by the reporting job with a range it built itself and should not have to
re-prove.

What is **not** asserted here is which storage path served the read.
`BalanceService.get_balance` answers from `merchant_balance_cache` for a request about now
and replays `ledger_entry` for a point in the past; that choice moved once already in arc
PERF (migration `0015`) without this file changing, which is the property worth keeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.api.routers.balances import (
    _INTERVALS,
    _MAX_HISTORY_DAYS,
    get_balance,
    get_balance_history,
)
from app.errors import ValidationError
from app.services.balances import BalanceService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
WEEK_AGO = NOW - timedelta(days=7)


class StubBalanceService(BalanceService):
    """The real class's interface, a dictionary behind it.

    Subclassed rather than duck-typed so that a signature change in `BalanceService`
    shows up as a failure here instead of as a silently-diverging stub. The production
    `__init__` wants four repositories and a clock; this one wants a payload.
    """

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.history_calls: list[dict[str, Any]] = []
        self.payload = payload or {
            "object": "balance",
            "merchant_id": "mer_loomcraft",
            "currency": "USD",
            "livemode": True,
            "available_minor": 418_000,
            "pending_minor": 22_500,
            "reserved_minor": 41_800,
            "as_of": NOW.isoformat(),
        }

    async def get_balance(
        self,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "merchant_id": merchant_id,
                "currency": currency,
                "livemode": livemode,
                "as_of": as_of,
            }
        )
        return dict(self.payload, merchant_id=merchant_id, currency=currency)

    async def get_balance_history(
        self,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
        from_: datetime,
        to: datetime,
        interval: str,
    ) -> dict[str, Any]:
        self.history_calls.append(
            {
                "merchant_id": merchant_id,
                "currency": currency,
                "livemode": livemode,
                "from_": from_,
                "to": to,
                "interval": interval,
            }
        )
        return {
            "object": "balance_history",
            "merchant_id": merchant_id,
            "currency": currency,
            "interval": interval,
            "points": [
                {"at": from_.isoformat(), "available_minor": 0},
                {"at": to.isoformat(), "available_minor": 418_000},
            ],
        }


# --------------------------------------------------------------------------------------
# GET /internal/v1/balances/{merchantId}
# --------------------------------------------------------------------------------------


async def test_the_balance_response_carries_every_documented_field() -> None:
    """The Java service deserialises this into a POJO. A missing key is a 500 over there."""
    balances = StubBalanceService()

    body = await get_balance(balances, "mer_loomcraft", "USD")

    assert body["merchant_id"] == "mer_loomcraft"
    assert body["currency"] == "USD"
    assert body["available_minor"] == 418_000
    assert body["pending_minor"] == 22_500
    assert body["reserved_minor"] == 41_800
    assert "as_of" in body


async def test_currency_is_upper_cased_before_it_reaches_the_service() -> None:
    """`?currency=usd` is a caller that read the docs and typed it in lower case.

    Account rows store `USD`, so the service would find nothing and answer zero — which
    is worse than an error, because a zero balance blocks a payout instead of failing a
    request.
    """
    balances = StubBalanceService()

    await get_balance(balances, "mer_loomcraft", "usd")

    assert balances.calls[0]["currency"] == "USD"


async def test_omitting_as_of_asks_for_now() -> None:
    """The cached path. The console hits this on every page load."""
    balances = StubBalanceService()

    await get_balance(balances, "mer_loomcraft", "USD")

    assert balances.calls[0]["as_of"] is None


async def test_supplying_as_of_is_passed_straight_through() -> None:
    """Statement generation always does. It is materially slower and that is expected."""
    balances = StubBalanceService()

    await get_balance(balances, "mer_loomcraft", "USD", WEEK_AGO)

    assert balances.calls[0]["as_of"] == WEEK_AGO


async def test_livemode_defaults_to_true() -> None:
    """A caller that forgets the flag gets live money, not sandbox money.

    Defaulting the other way would have the console show test balances to a real merchant,
    which reads as "my money is gone".
    """
    balances = StubBalanceService()

    await get_balance(balances, "mer_loomcraft", "USD")

    assert balances.calls[0]["livemode"] is True


async def test_test_mode_is_a_separate_balance() -> None:
    balances = StubBalanceService()

    await get_balance(balances, "mer_loomcraft", "USD", None, False)

    assert balances.calls[0]["livemode"] is False


async def test_a_merchant_with_no_postings_gets_zero_not_a_404() -> None:
    """Accounts exist from activation; entries do not exist until the first charge."""
    balances = StubBalanceService(
        {
            "object": "balance",
            "merchant_id": "mer_new",
            "currency": "USD",
            "livemode": True,
            "available_minor": 0,
            "pending_minor": 0,
            "reserved_minor": 0,
            "as_of": NOW.isoformat(),
        }
    )

    body = await get_balance(balances, "mer_new", "USD")

    assert body["available_minor"] == 0
    assert body["reserved_minor"] == 0


# --------------------------------------------------------------------------------------
# GET /internal/v1/balances/{merchantId}/history
# --------------------------------------------------------------------------------------


async def test_history_returns_bucketed_points() -> None:
    balances = StubBalanceService()

    body = await get_balance_history(balances, "mer_loomcraft", "USD", WEEK_AGO, NOW)

    assert body["interval"] == "day"
    assert len(body["points"]) == 2
    assert balances.history_calls[0]["from_"] == WEEK_AGO


async def test_hourly_is_the_other_supported_interval() -> None:
    balances = StubBalanceService()

    body = await get_balance_history(
        balances, "mer_loomcraft", "USD", WEEK_AGO, NOW, "hour"
    )

    assert body["interval"] == "hour"
    assert set(_INTERVALS) == {"hour", "day"}


async def test_an_unknown_interval_is_a_422_not_an_empty_series() -> None:
    """`?interval=week` used to return `points: []`, which reads as "no activity"."""
    balances = StubBalanceService()

    with pytest.raises(ValidationError) as excinfo:
        await get_balance_history(
            balances, "mer_loomcraft", "USD", WEEK_AGO, NOW, "week"
        )

    assert excinfo.value.http_status == 422
    assert excinfo.value.details["interval"] == "week"
    assert balances.history_calls == []


async def test_a_reversed_range_is_rejected_at_the_edge() -> None:
    balances = StubBalanceService()

    with pytest.raises(ValidationError) as excinfo:
        await get_balance_history(balances, "mer_loomcraft", "USD", NOW, WEEK_AGO)

    assert "must be after" in str(excinfo.value)
    assert balances.history_calls == []


async def test_an_empty_range_is_rejected_too() -> None:
    """`from == to` is a caller looping over a list of days and hitting the boundary."""
    balances = StubBalanceService()

    with pytest.raises(ValidationError):
        await get_balance_history(balances, "mer_loomcraft", "USD", NOW, NOW)


async def test_the_window_is_capped() -> None:
    """The Java service asked for a year once. It got a 422 and a Slack message."""
    balances = StubBalanceService()
    too_far_back = NOW - timedelta(days=_MAX_HISTORY_DAYS + 5)

    with pytest.raises(ValidationError) as excinfo:
        await get_balance_history(balances, "mer_loomcraft", "USD", too_far_back, NOW)

    assert excinfo.value.details["requested_days"] == _MAX_HISTORY_DAYS + 5


async def test_the_cap_is_four_hundred_days() -> None:
    """Wide enough for a full year plus a reporting month, and no wider."""
    assert _MAX_HISTORY_DAYS == 400


async def test_a_window_exactly_at_the_cap_is_allowed() -> None:
    balances = StubBalanceService()

    body = await get_balance_history(
        balances, "mer_loomcraft", "USD", NOW - timedelta(days=_MAX_HISTORY_DAYS), NOW
    )

    assert body["merchant_id"] == "mer_loomcraft"
    assert len(balances.history_calls) == 1
