"""Deferred capture bookkeeping.

Eleven travel and lodging merchants run ``capture_at_settlement``: payzeno-api
authorises the card, leaves it uncaptured, and the ledger issues the capture when the
acquirer's settlement line arrives. That makes the ledger — not the API — the component
holding an irreversible cardholder side effect.

This service owns the ``capture_attempt`` ledger of those calls: what we asked for, with
which acquirer idempotency key, and what came back. ``DeferredCaptureJob`` drives it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.idempotency import ledger_key
from app.errors import ProcessorIndeterminateError, ProcessorUnavailableError
from app.logging import get_logger
from app.metrics import metrics
from app.models.capture_attempt import CaptureAttempt
from app.ports import Clock, ProcessorClient, SessionFactory
from app.repositories.capture_attempt import CaptureAttemptRepository

logger = get_logger(__name__)


class DeferredCaptureService:
    """Records, issues and resolves deferred captures."""

    def __init__(
        self,
        sessions: SessionFactory,
        attempts: CaptureAttemptRepository,
        processor: ProcessorClient,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._attempts = attempts
        self._processor = processor
        self._clock = clock

    async def record_intent(
        self,
        session: AsyncSession,
        *,
        charge_id: str,
        item_id: str | None,
        acquirer: str,
        batch_id: str,
        amount_minor: int,
        currency: str,
        livemode: bool,
    ) -> CaptureAttempt | None:
        """Claim the right to capture this charge, before any HTTP call.

        ``uq_capture_attempt_key`` is the claim. A second caller loses the INSERT and
        gets ``None`` back, which means "someone else owns this capture" — not "capture
        it again".
        """
        key = ledger_key("capture", batch_id, charge_id)
        attempt = CaptureAttempt(
            id=f"cap_{key[-24:]}",
            charge_id=charge_id,
            item_id=item_id,
            acquirer=acquirer,
            acquirer_idempotency_key=key,
            amount_minor=amount_minor,
            currency=currency,
            status="pending",
            livemode=livemode,
            requested_at=self._clock.now(),
        )
        try:
            await self._attempts.add(session, attempt)
            await session.flush()
        except IntegrityError:
            await session.rollback()
            logger.info(
                "capture_attempt_already_claimed",
                charge_id=charge_id,
                acquirer_idempotency_key=key,
            )
            return None
        return attempt

    async def process_pending(self, *, limit: int = 100) -> int:
        """Issue or resolve every pending / indeterminate capture attempt.

        Each attempt gets its own transaction and the acquirer call happens *between*
        two committed states, never inside a transaction that also writes the ledger.
        """
        async with self._sessions.begin() as session:
            pending = await self._attempts.list_pending(session, limit=limit)
            attempt_ids = [(attempt.id, attempt.status) for attempt in pending]

        processed = 0
        for attempt_id, status in attempt_ids:
            if status == "indeterminate":
                await self._resolve_indeterminate(attempt_id)
            else:
                await self._issue(attempt_id)
            processed += 1

        logger.info("deferred_capture_pass", processed=processed, limit=limit)
        return processed

    async def _issue(self, attempt_id: str) -> None:
        async with self._sessions.begin() as session:
            attempt = await self._attempts.get_or_raise(session, attempt_id)
            charge_id = attempt.charge_id
            key = attempt.acquirer_idempotency_key
            amount_minor = attempt.amount_minor
            currency = attempt.currency

        outcome: tuple[str, str | None, str | None]
        try:
            response = await self._processor.capture_deferred(
                charge_id=charge_id,
                amount_minor=amount_minor,
                currency=currency,
                reference=key,
                idempotency_key=key,
            )
            outcome = ("captured" if response.captured else "failed", response.reference, None)
        except ProcessorIndeterminateError as exc:
            outcome = ("indeterminate", None, getattr(exc, "code", "processor_timeout"))
        except ProcessorUnavailableError as exc:
            outcome = ("pending", None, getattr(exc, "code", "processor_unavailable"))

        await self._apply_outcome(attempt_id, outcome)

    async def _resolve_indeterminate(self, attempt_id: str) -> None:
        """Never re-issue an indeterminate capture. Ask what happened instead."""
        async with self._sessions.begin() as session:
            attempt = await self._attempts.get_or_raise(session, attempt_id)
            acquirer = attempt.acquirer
            key = attempt.acquirer_idempotency_key

        status = await self._processor.get_capture_status(
            acquirer=acquirer, idempotency_key=key
        )
        if status.state == "captured":
            await self._apply_outcome(attempt_id, ("captured", status.reference, None))
        elif status.state == "not_captured":
            await self._apply_outcome(attempt_id, ("pending", None, "not_captured"))
        else:
            logger.warning(
                "capture_status_unknown", attempt_id=attempt_id, acquirer=acquirer
            )

    async def _apply_outcome(
        self, attempt_id: str, outcome: tuple[str, str | None, str | None]
    ) -> None:
        status, reference, error_code = outcome
        async with self._sessions.begin() as session:
            attempt = await self._attempts.get_or_raise(session, attempt_id)
            attempt.status = status
            attempt.response_reference = reference
            attempt.last_error_code = error_code
            if status in ("captured", "failed"):
                attempt.completed_at = self._clock.now()
        metrics.increment("DeferredCaptureOutcome", status=status)
        logger.info(
            "capture_attempt_updated",
            attempt_id=attempt_id,
            status=status,
            error_code=error_code,
        )

    async def unmatched_since(
        self, session: AsyncSession, since: datetime
    ) -> list[CaptureAttempt]:
        """Capture attempts with no matching settle transaction. Read by the audit job."""
        return await self._attempts.list_unmatched(session, since=since)
