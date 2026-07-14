"""Worldflow acquirer client — implements ``app.ports.ProcessorClient``.

Route shape is frozen in ``api-surface.md`` §15 and is identical for both acquirers;
only the base URL, the API key and the acquirer account header differ. See
``app/clients/nordpay.py`` for the sibling.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.clients.http import LedgerHttpxClient
from app.config import Settings
from app.errors import ProcessorIndeterminateError, UpstreamError
from app.logging import get_logger
from app.ports import CaptureResponse, CaptureStatus, ProcessorClient

logger = get_logger(__name__)

ACQUIRER = "worldflow"


class WorldflowClient(ProcessorClient):
    """Talks to Worldflow's ``/v2`` surface.

    Every mutating call carries an explicit ``Idempotency-Key``. Worldflow honours it
    for 24 hours and replays the original result, which is what makes a *retry* of a
    capture safe — and why an indeterminate outcome must be resolved through
    :meth:`get_capture_status` rather than by re-issuing the capture.
    """

    acquirer = ACQUIRER

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = LedgerHttpxClient(
            base_url=settings.worldflow_base_url,
            api_key=settings.worldflow_api_key,
            acquirer=ACQUIRER,
            acquirer_account=settings.worldflow_acquirer_account,
        )

    async def confirm_settlement(
        self, acquirer: str, acquirer_reference: str, batch_id: str
    ) -> None:
        """Acknowledge receipt of one settlement line.

        Called by every reconciliation item regardless of line type and regardless of
        whether the merchant defers capture. It is therefore the call whose failure
        backs up a whole batch rather than a handful of merchants.
        """
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
        """Capture an authorisation that was deliberately left uncaptured until settlement.

        ``reference`` is the acquirer's network transaction id for the authorisation —
        the value Worldflow keys its own authorisation record on.
        """
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
        """Ask Worldflow what actually happened to a capture we are unsure about.

        The authorisation id is embedded in the idempotency key we minted
        (``capture:<batch_id>:<charge_id>``); Worldflow indexes the key directly, so
        the path segment is the key itself.
        """
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
        """Download the raw settlement file for a processing date.

        Worldflow files are CSV; ``WorldflowCsvParser`` owns the format.
        """
        response = await self._http.get(
            "/v2/settlement-files", params={"date": processing_date.isoformat()}
        )
        return response.content

    async def health_check(self) -> bool:
        """Breaker probe. Never raises — a probe that raises re-opens the circuit."""
        try:
            await self._http.get("/v2/health")
        except UpstreamError:
            return False
        except ProcessorIndeterminateError:
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
