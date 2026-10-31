"""`HttpFxRateProvider` (app/clients/fx.py) and `FxRateService` (app/services/fx.py).

The provider is sim-world's simulated feed locally and a vendor in production, behind one
URL shape: `GET {FX_PROVIDER_URL}/v1/rates?base=USD`. The client is driven here through
`httpx.MockTransport`, so the request it builds and the body it parses are both real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.clients.fx import HttpFxRateProvider
from app.config import Settings
from app.domain.money import Money
from app.errors import FxRateUnavailableError
from app.ports import FxRates
from app.services.fx import CACHE_TTL, FxRateService
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)

SIM_BODY = {
    "base": "USD",
    "as_of": "2026-10-29T08:59:00Z",
    "rates": {
        "EUR": "0.9231",
        "GBP": "0.7902",
        "ILS": "3.7120",
        "JPY": "151.20",
        "USD": "1",
        "CHF": "0.8812",
    },
}


def _provider(handler: Any) -> HttpFxRateProvider:
    settings = Settings(fx_provider_url="http://sim-fx:3200/")
    return HttpFxRateProvider(settings, transport=httpx.MockTransport(handler))


async def test_it_asks_for_usd_based_rates_at_the_contracted_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SIM_BODY)

    rates = await _provider(handler).latest("USD")

    assert seen[0].url.path == "/v1/rates"
    assert seen[0].url.params["base"] == "USD"
    assert seen[0].url.host == "sim-fx"
    assert rates.base == "USD"
    assert rates.as_of == datetime(2026, 10, 29, 8, 59, tzinfo=UTC)


async def test_rates_are_exact_decimals_and_unsupported_currencies_are_dropped() -> None:
    rates = await _provider(lambda _: httpx.Response(200, json=SIM_BODY)).latest("USD")

    assert rates.rates == {
        "EUR": Decimal("0.9231"),
        "GBP": Decimal("0.7902"),
        "ILS": Decimal("3.7120"),
        "JPY": Decimal("151.20"),
        "USD": Decimal("1"),
    }


async def test_json_numbers_keep_their_literal_digits() -> None:
    """A vendor that sends numbers must not hand us `0.92310000000000003`."""
    body = json.dumps({"base": "USD", "as_of": "2026-10-29T09:00:00Z", "rates": {"EUR": 0.9231}})

    rates = await _provider(lambda _: httpx.Response(200, text=body)).latest("USD")

    assert rates.rates["EUR"] == Decimal("0.9231")
    assert rates.rates["USD"] == Decimal(1)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": "down"}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"base": "EUR", "as_of": "2026-10-29T09:00:00Z", "rates": {}}),
        httpx.Response(200, json={"base": "USD", "rates": {"EUR": "0.9"}}),
        httpx.Response(200, json={"base": "USD", "as_of": "x", "rates": {"EUR": "-1"}}),
    ],
    ids=["5xx", "not-json", "wrong-base", "no-as-of", "negative-rate"],
)
async def test_anything_that_is_not_a_rate_table_is_unavailable(response: httpx.Response) -> None:
    with pytest.raises(FxRateUnavailableError):
        await _provider(lambda _: response).latest("USD")


async def test_an_unreachable_provider_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(FxRateUnavailableError):
        await _provider(handler).latest("USD")


# -- FxRateService ------------------------------------------------------------------------


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def latest(self, base: str) -> FxRates:
        self.calls += 1
        return FxRates(
            base=base,
            as_of=NOW,
            rates={"USD": Decimal(1), "EUR": Decimal("0.9231"), "JPY": Decimal("151.20")},
        )


async def test_payout_rate_crosses_through_usd_and_carries_the_markup() -> None:
    service = FxRateService(CountingProvider(), FrozenClock(NOW), payout_markup_bps=100)

    rate = await service.payout_rate("EUR", "USD")

    assert rate.mid_rate == Decimal("1.0833062507")
    assert rate.markup_bps == 100
    assert rate.as_of == NOW
    conversion = service.convert(Money(10_000, "EUR"), rate)
    assert conversion.target == Money(10_725, "USD")
    assert conversion.spread_minor == 108


async def test_one_rate_table_serves_a_burst_of_payouts() -> None:
    provider = CountingProvider()
    clock = FrozenClock(NOW)
    service = FxRateService(provider, clock, payout_markup_bps=100)

    await service.payout_rate("EUR", "USD")
    await service.payout_rate("JPY", "EUR")
    clock.advance(30)
    await service.payout_rate("USD", "JPY")

    assert provider.calls == 1


async def test_the_cache_follows_the_injected_clock_not_the_wall() -> None:
    """Simulated time jumps days; a cached rate from before the jump is not reused."""
    provider = CountingProvider()
    clock = FrozenClock(NOW)
    service = FxRateService(provider, clock, payout_markup_bps=100)

    await service.payout_rate("EUR", "USD")
    clock.advance(CACHE_TTL.total_seconds())
    await service.payout_rate("EUR", "USD")
    clock.set(NOW - timedelta(days=1))
    await service.payout_rate("EUR", "USD")

    assert provider.calls == 3
