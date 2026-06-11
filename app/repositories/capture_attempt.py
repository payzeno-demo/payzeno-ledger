"""``capture_attempt`` data access — the control that sits on the double-capture path.

``uq_charge_processor_reference`` in ``payzeno_api`` is not a defence against the incident:
it lives in a different database, and the duplicate capture is issued from this service,
which never writes ``charge``. This table is the control that is actually on the call path.

The row is **inserted and committed before** the acquirer call and updated after, under
``uq_capture_attempt_key (acquirer, acquirer_idempotency_key)``. A second capture therefore
fails on insert before any HTTP request leaves the process. ``DeferredCaptureJob`` (30s)
drives the ``pending`` rows, which is what makes the external call happen *outside* the
business transaction — a transaction that aborts after the claim cannot leave a charged
cardholder with no ledger row.

``indeterminate`` rows are resolved by ``ProcessorClient.get_capture_status``, never by
re-issuing the capture. A timeout on a capture is the one state where you do not know
whether the cardholder was charged, and retrying it is the second, independent
double-charge mechanism that PR #172 did nothing about. PAY-2060, migration ``0025``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.capture_attempt import CaptureAttempt
from app.repositories.base import BaseRepository

#: Statuses the deferred-capture job still owns. Covered by
#: ``pix_capture_attempt_pending``.
OPEN_STATUSES: tuple[str, ...] = ("pending", "indeterminate")

#: Anything older than this in `pending` is an invariant (6) violation and the audit job
#: says so out loud.
STALE_AFTER = dt.timedelta(hours=1)


class CaptureAttemptRepository(BaseRepository[CaptureAttempt]):
    """Reads and writes the deferred-capture attempt ledger."""

    model: ClassVar[type[CaptureAttempt]] = CaptureAttempt
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return CaptureAttempt.requested_at

    async def find_by_key(
        self, session: AsyncSession, *, acquirer: str, acquirer_idempotency_key: str
    ) -> CaptureAttempt | None:
        """Look up an attempt by the key we sent the acquirer.

        Both acquirers honour that key, so this is also how a *their-side* duplicate is
        identified when support asks. It is a read: claiming is done by inserting and
        letting ``uq_capture_attempt_key`` decide, never by reading first.
        """
        stmt = (
            select(CaptureAttempt)
            .where(CaptureAttempt.acquirer == acquirer)
            .where(CaptureAttempt.acquirer_idempotency_key == acquirer_idempotency_key)
        )
        return (await session.execute(stmt)).scalars().first()

    async def list_pending(
        self,
        session: AsyncSession,
        *,
        limit: int = 100,
        now: dt.datetime | None = None,
    ) -> list[CaptureAttempt]:
        """Open attempts, oldest request first.

        ``pending`` rows have not been sent yet; ``indeterminate`` rows were sent and we
        do not know the outcome. The job handles them differently and reads them together,
        because the ordering that matters is "how long has a cardholder been in limbo",
        not which kind of limbo it is.
        """
        stmt = (
            select(CaptureAttempt)
            .where(CaptureAttempt.status.in_(OPEN_STATUSES))
            .order_by(CaptureAttempt.requested_at)
            .limit(limit)
        )
        if now is not None:
            stmt = stmt.where(CaptureAttempt.requested_at <= now)
        return list((await session.execute(stmt)).scalars().all())

    async def list_for_charge(
        self, session: AsyncSession, charge_id: str
    ) -> list[CaptureAttempt]:
        """Every capture attempt against one charge, oldest first.

        Should be one row. Two rows with different keys means two ledger items asked to
        capture the same charge, and that is the shape of the incident — this query is in
        `docs/runbooks/reconciliation.md` for exactly that reason.
        """
        stmt = (
            select(CaptureAttempt)
            .where(CaptureAttempt.charge_id == charge_id)
            .order_by(CaptureAttempt.requested_at)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_for_item(
        self, session: AsyncSession, item_id: str
    ) -> list[CaptureAttempt]:
        """Attempts linked to one reconciliation item.

        ``item_id`` is nullable — a capture can be issued outside reconciliation — so this
        is a filtered read rather than a lookup by key.
        """
        stmt = (
            select(CaptureAttempt)
            .where(CaptureAttempt.item_id == item_id)
            .order_by(CaptureAttempt.requested_at)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def mark_outcome(
        self,
        session: AsyncSession,
        attempt_id: str,
        *,
        status: str,
        response_reference: str | None = None,
        last_error_code: str | None = None,
        completed_at: dt.datetime | None = None,
    ) -> CaptureAttempt:
        """Record what the acquirer said.

        ``completed_at`` is stamped only for terminal states. An ``indeterminate`` attempt
        is emphatically not complete: it is a row somebody has to resolve, and leaving
        ``completed_at`` null is what keeps it in :meth:`list_pending` until they do.
        """
        attempt = await self.get_or_raise(session, attempt_id)
        attempt.status = status
        attempt.response_reference = response_reference
        attempt.last_error_code = last_error_code
        if status in ("captured", "failed"):
            attempt.completed_at = completed_at or dt.datetime.now(dt.UTC)
        await session.flush()
        return attempt

    async def list_unmatched(
        self,
        session: AsyncSession,
        *,
        since: dt.datetime,
        limit: int = 200,
    ) -> list[CaptureAttempt]:
        """Captured attempts with no settling transaction behind them.

        Invariant (6) of `data-model.md` §6, checked nightly: every ``captured`` attempt
        must have exactly one ``settle`` transaction for its item. A row here means a
        cardholder was charged and the ledger has no record of the settlement — which is
        the failure the whole ``capture_attempt`` table was added to make visible, since
        before it the only evidence was in the acquirer's portal.

        The join is left to the audit service, which holds the transaction repository;
        this returns the candidate set inside the window.
        """
        stmt = (
            select(CaptureAttempt)
            .where(CaptureAttempt.status == "captured")
            .where(CaptureAttempt.requested_at >= since)
            .order_by(CaptureAttempt.requested_at)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def count_stale_pending(
        self, session: AsyncSession, *, now: dt.datetime | None = None
    ) -> int:
        """How many open attempts are older than :data:`STALE_AFTER`.

        Exported as a gauge and alarmed on at anything above zero. An hour is generous:
        the job runs every thirty seconds.
        """
        cutoff = (now or dt.datetime.now(dt.UTC)) - STALE_AFTER
        stmt = (
            select(func.count())
            .select_from(CaptureAttempt)
            .where(CaptureAttempt.status.in_(OPEN_STATUSES))
            .where(CaptureAttempt.requested_at < cutoff)
        )
        return int((await session.execute(stmt)).scalar_one())
