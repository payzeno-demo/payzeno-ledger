"""Nordpay acquirer client — implements ``app.ports.ProcessorClient``.

Same ``/v2`` route shape as Worldflow (``api-surface.md`` §15), different base URL and
credentials. The one real difference lives outside this module: Nordpay still files
fixed-width settlement files, which is why ``LegacyFixedWidthParser`` exists and has no
retirement date.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.clients.http import LedgerHttpxClient
from app.config import Settings
from app.errors import ProcessorIndeterminateError, UpstreamError
from app.logging import get_logger
from app.ports import CaptureResponse, CaptureStatus, ProcessorClient

logger = get_logger(__name__)

ACQUIRER = "nordpay"


class NordpayClient(ProcessorClient):
    """Talks to Nordpay's ``/v2`` surface."""

    acquirer = ACQUIRER

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = LedgerHttpxClient(
            base_url=settings.nordpay_base_url,
            api_key=settings.nordpay_api_key,
            acquirer=ACQUIRER,
            acquirer_account=settings.nordpay_acquirer_account,
            # Nordpay's settlement file endpoint is slow on month-end; the read timeout
            # is deliberately wider than Worldflow's.
            read_timeout_seconds=20.0,
        )

    async def confirm_settlement(
        self, acquirer: str, acquirer_reference: str, batch_id: str
    ) -> None:
        await self._http.post(
            f"/v2/settlement-files/{batch_id}/confirm",
            json={
                "acquirer_reference": acquirer_reference,
                "confirmed_by": "payzeno-ledger",
            },
            headers={"Idempotency-Key": f"confirm:{batch_id}:{acquirer_reference}"},
        )

    async def capture_deferred(
        self,
        charge_id: str,
        amount_minor: int,
        currency: str,
        reference: str,
        *,
        idempotency_key: str,
    ) -> CaptureResponse:
        response = await self._http.post(
            f"/v2/authorizations/{reference}/captures",
            json={
                "amount": amount_minor,
                "currency": currency,
                "merchant_reference": charge_id,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        body = response.json()
        return CaptureResponse(
            captured=bool(body.get("captured", True)),
            reference=str(body.get("reference") or idempotency_key),
            captured_at=_parse_timestamp(body.get("captured_at")),
        )

    async def get_capture_status(self, acquirer: str, idempotency_key: str) -> CaptureStatus:
        try:
            response = await self._http.get(
                f"/v2/authorizations/{idempotency_key}/captures/{idempotency_key}"
            )
        except UpstreamError as exc:
            if exc.details.get("status") == 404:
                return CaptureStatus(state="not_captured", reference=None)
            raise
        body = response.json()
        state = body.get("state", "unknown")
        if state not in ("captured", "not_captured"):
            state = "unknown"
        return CaptureStatus(state=state, reference=body.get("reference"))

    async def fetch_settlement_file(self, acquirer: str, processing_date: date) -> bytes:
        response = await self._http.get(
            "/v2/settlement-files", params={"date": processing_date.isoformat()}
        )
        return response.content

    async def health_check(self) -> bool:
        try:
            await self._http.get("/v2/health")
        except (UpstreamError, ProcessorIndeterminateError):
            return False
        return True

    async def aclose(self) -> None:
        await self._http.aclose()


def _parse_timestamp(raw: object) -> datetime:
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("acquirer_bad_timestamp", acquirer=ACQUIRER, value=raw)
    return datetime.now(tz=timezone.utc)
