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
            acquirer=ACQUIRER,
            acquirer_account=settings.nordpay_acquirer_account,
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
    return datetime.now(tz=timezone.utc)
