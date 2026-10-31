"""FX rates client — implements ``app.ports.FxRateProvider``.

``GET {FX_PROVIDER_URL}/v1/rates?base=USD`` →
``{"base": "USD", "as_of": "<iso>", "rates": {"EUR": "0.9231", "JPY": "151.20", "USD": "1"}}``.

A vendor in production, sim-world's simulated feed (``http://sim-fx:3200``) locally, and the
same shape both ways. Rates are decimal strings on the wire, like money; the body is parsed
with ``parse_float=Decimal`` anyway, so a provider that sends JSON numbers keeps its literal
digits instead of a binary-float approximation.

No retry, no breaker. A payout is the only caller and it is a request-path call a human
or the sweep will repeat; a rate that cannot be fetched is :class:`FxRateUnavailableError`
and the payout is refused before anything is written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings
from app.domain.fx import parse_rate
from app.domain.money import SUPPORTED_CURRENCIES
from app.errors import FxRateUnavailableError, ValidationError
from app.logging import get_logger
from app.ports import FxRateProvider, FxRates

logger = get_logger(__name__)


class HttpFxRateProvider(FxRateProvider):
    """One pooled ``httpx.AsyncClient`` against ``FX_PROVIDER_URL``."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = settings.fx_provider_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=read_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            headers={"Accept": "application/json", "User-Agent": "payzeno-ledger/1.31"},
            transport=transport,
        )

    async def latest(self, base: str) -> FxRates:
        try:
            response = await self._client.get("/v1/rates", params={"base": base})
        except httpx.HTTPError as exc:
            logger.warning(
                "fx_rates_unreachable", provider=self._base_url, error=type(exc).__name__
            )
            raise FxRateUnavailableError(
                "fx provider unreachable", provider=self._base_url, base=base
            ) from exc
        if response.status_code != 200:
            logger.warning(
                "fx_rates_rejected", provider=self._base_url, status=response.status_code
            )
            raise FxRateUnavailableError(
                f"fx provider returned {response.status_code}",
                provider=self._base_url,
                base=base,
                status=response.status_code,
            )
        try:
            body = json.loads(response.text, parse_float=Decimal)
            rates = _parse_body(body, base=base)
        except (ValueError, ValidationError) as exc:
            logger.warning("fx_rates_malformed", provider=self._base_url, error=str(exc)[:200])
            raise FxRateUnavailableError(
                "fx provider sent a malformed rate table", provider=self._base_url, base=base
            ) from exc
        logger.info(
            "fx_rates_fetched",
            base=rates.base,
            as_of=rates.as_of.isoformat(),
            currencies=sorted(rates.rates),
        )
        return rates

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_body(body: Any, *, base: str) -> FxRates:
    """The provider body as :class:`FxRates`, or :class:`ValidationError`.

    Currencies Payzeno does not support are dropped, not rejected: a vendor table carries
    170 of them and the five we use are what matters. The base must come back as asked.
    """
    if not isinstance(body, dict) or not isinstance(body.get("rates"), dict):
        raise ValidationError("fx body has no rates object")
    if str(body.get("base", "")).upper() != base:
        raise ValidationError("fx body is for another base", base=body.get("base"), wanted=base)
    rates = {
        code.upper(): parse_rate(raw, currency=code)
        for code, raw in body["rates"].items()
        if isinstance(code, str) and code.upper() in SUPPORTED_CURRENCIES
    }
    rates.setdefault(base, Decimal(1))
    return FxRates(base=base, as_of=_parse_as_of(body.get("as_of")), rates=rates)


def _parse_as_of(raw: object) -> datetime:
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("fx as_of is not an ISO timestamp", as_of=raw) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise ValidationError("fx body has no as_of")
