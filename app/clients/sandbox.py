"""Sandbox processor — the ``ProcessorClient`` used for ``livemode = false`` merchants.

This is wired in production. Test-mode merchants run the whole settlement path end to
end, including the ledger postings, but no request ever leaves the VPC. Without it a
test-mode batch would confirm lines against a real acquirer account.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from app.errors import ProcessorUnavailableError
from app.logging import get_logger
from app.ports import CaptureResponse, CaptureStatus, ProcessorClient

logger = get_logger(__name__)

# A tiny fixed-width sample so SettlementImportJob has something to parse in sandbox.
_SAMPLE_FILE = (
    b"acquirer_reference,network_reference,gross_minor,fee_minor,"
    b"interchange_minor,scheme_fee_minor,net_minor,currency,line_type\n"
    b"SBX-0001,ntx_sandbox_0001,10000,290,180,40,9710,USD,sale\n"
    b"SBX-0002,ntx_sandbox_0002,2500,95,60,15,2405,USD,sale\n"
    b"SBX-0003,ntx_sandbox_0003,-1500,0,0,0,-1500,USD,refund\n"
)


class SandboxProcessorClient(ProcessorClient):
    """Deterministic-ish stand-in for an acquirer.

    TODO: sandbox declines are random; make them deterministic. Right now a merchant
    integrating against test mode cannot write an assertion that survives two runs,
    which is the single most common complaint in support tickets tagged `sandbox`.
    """

    acquirer = "sandbox"

    def __init__(self, *, decline_every: int = 17) -> None:
        self._decline_every = decline_every
        self._captures: dict[str, CaptureResponse] = {}
        self._calls = 0

    async def confirm_settlement(
        self, acquirer: str, acquirer_reference: str, batch_id: str
    ) -> None:
        self._calls += 1
        logger.debug(
            "sandbox_confirm_settlement",
            acquirer=acquirer,
            batch_id=batch_id,
            acquirer_reference=acquirer_reference,
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
        existing = self._captures.get(idempotency_key)
        if existing is not None:
            # Mirrors both acquirers' 24h idempotency replay.
            return existing

        self._calls += 1
        if self._should_decline(idempotency_key):
            raise ProcessorUnavailableError(
                "sandbox synthetic decline",
                code="temporary_failure",
                acquirer=self.acquirer,
                charge_id=charge_id,
            )

        response = CaptureResponse(
            captured=True,
            reference=f"sbxcap_{idempotency_key[-16:]}",
            captured_at=datetime.now(tz=timezone.utc),
        )
        self._captures[idempotency_key] = response
        return response

    async def get_capture_status(self, acquirer: str, idempotency_key: str) -> CaptureStatus:
        existing = self._captures.get(idempotency_key)
        if existing is None:
            return CaptureStatus(state="not_captured", reference=None)
        return CaptureStatus(state="captured", reference=existing.reference)

    async def fetch_settlement_file(self, acquirer: str, processing_date: date) -> bytes:
        logger.debug(
            "sandbox_fetch_settlement_file",
            acquirer=acquirer,
            processing_date=processing_date.isoformat(),
        )
        return _SAMPLE_FILE

    async def health_check(self) -> bool:
        return True

    def _should_decline(self, idempotency_key: str) -> bool:
        digest = hashlib.blake2b(idempotency_key.encode("utf-8"), digest_size=2).digest()
        return int.from_bytes(digest, "big") % self._decline_every == 0
