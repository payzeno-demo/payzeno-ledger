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
            acquirer=ACQUIRER,
            json={
                "acquirer_reference": acquirer_reference,
                "confirmed_by": "payzeno-ledger",
            },
            json={
                "amount": amount_minor,
                "currency": currency,
                "merchant_reference": charge_id,
            },
    return datetime.now(tz=timezone.utc)
