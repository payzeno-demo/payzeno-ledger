"""FX rates for payouts — RFC platform-v2 §7.

Wraps the ``FxRateProvider`` seam with the two things a payout needs and the provider does
not know about: a short cache, and Payzeno's payout markup (``FX_PAYOUT_MARKUP_BPS``).

The cache is keyed on the provider's base and timed off the injected :class:`Clock`, not
the wall clock — locally the whole stack runs on simulated time (RFC §10), and a cache
that disagreed with the ledger about what "now" is would hand a payout a rate from a
simulated week ago.

One rate table serves every currency pair: the provider quotes everything against USD and
:func:`app.domain.fx.cross_rate` crosses through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from app.domain.fx import FxConversion, cross_rate, quote_payout_conversion
from app.domain.money import Money
from app.logging import get_logger
from app.ports import Clock, FxRateProvider, FxRates

logger = get_logger(__name__)

#: The provider quotes every rate against this. Not configurable: it is the shape of the
#: vendor contract, and the sim feed mirrors it.
PROVIDER_BASE: Final[str] = "USD"

#: How long one rate table is reused. A payout sweep converts a few hundred balances in a
#: burst; one fetch per burst is the point, and a minute is well inside the drift anyone
#: would notice on a payout.
CACHE_TTL: Final[timedelta] = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class PayoutRate:
    """What a payout converts at, and where the number came from."""

    from_currency: str
    to_currency: str
    mid_rate: Decimal
    markup_bps: int
    as_of: datetime


class FxRateService:
    """Prices payout conversions. One instance, built in ``app/container.py``."""

    def __init__(self, provider: FxRateProvider, clock: Clock, *, payout_markup_bps: int) -> None:
        self._provider = provider
        self._clock = clock
        self._payout_markup_bps = payout_markup_bps
        self._cached: tuple[datetime, FxRates] | None = None

    async def payout_rate(self, from_currency: str, to_currency: str) -> PayoutRate:
        """The mid-rate for ``from → to`` and the markup a payout applies to it.

        Raises :class:`~app.errors.FxRateUnavailableError` (from the provider) when there is
        no rate table to be had. Fetch this *before* taking the payout lock: it may be an
        HTTP round trip, and nothing about the rate depends on the merchant's balance.
        """
        rates = await self._rates()
        return PayoutRate(
            from_currency=from_currency,
            to_currency=to_currency,
            mid_rate=cross_rate(rates.rates, from_currency, to_currency),
            markup_bps=self._payout_markup_bps,
            as_of=rates.as_of,
        )

    @staticmethod
    def convert(source: Money, rate: PayoutRate) -> FxConversion:
        """Apply a :class:`PayoutRate` to an amount of its source currency."""
        return quote_payout_conversion(
            source, rate.to_currency, mid_rate=rate.mid_rate, markup_bps=rate.markup_bps
        )

    async def _rates(self) -> FxRates:
        now = self._clock.now()
        if self._cached is not None:
            fetched_at, rates = self._cached
            # A clock that moved backwards (a simulated-time reset) invalidates too.
            if fetched_at <= now < fetched_at + CACHE_TTL:
                return rates
        rates = await self._provider.latest(PROVIDER_BASE)
        self._cached = (now, rates)
        return rates
