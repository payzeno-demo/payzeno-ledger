"""``reconciliation_run`` data access — one row per pass over one batch.

A run is bookkeeping, not control flow: nothing waits on it and nothing locks on it. The
mutual exclusion between two sweeps is the batch advisory lock in ``app/db/locks.py``, and
``pix_reconciliation_run_active`` — partial, on ``status = 'running'`` — is only there so
the ops console can answer "is something working this batch right now" without a scan.

That distinction is worth keeping in mind: on the night of PAY-2041 there was exactly one
``running`` run per batch the whole time, and the duplicate came from a *retry* that never
opened a run at all.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ids import new_id
from app.errors import NotFoundError
from app.models.reconciliation_run import ReconciliationRun
from app.repositories.base import BaseRepository


class ReconciliationRunRepository(BaseRepository[ReconciliationRun]):
    """Opens, closes and reads ``reconciliation_run`` rows."""

    model: ClassVar[type[ReconciliationRun]] = ReconciliationRun
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return ReconciliationRun.id

    async def start(
        self,
        session: AsyncSession,
        *,
        batch_id: str,
        trigger: str,
        at: dt.datetime | None = None,
    ) -> ReconciliationRun:
        """Open a run and return it.

        Called in its own short transaction, before the guard session takes the batch
        advisory lock. Deliberately so: the run row is what an operator watches while the
        pass is blocked waiting for that lock, and a run created *inside* the guard
        transaction would only become visible once the pass had already started.
        """
        run = ReconciliationRun(
            id=new_id("rr"),
            batch_id=batch_id,
            trigger=trigger,
            status="running",
            items_total=0,
            items_settled=0,
            items_failed=0,
            started_at=at or dt.datetime.now(dt.UTC),
        )
        session.add(run)
        await session.flush()
        return run

    async def finish(
        self,
        session: AsyncSession,
        run_id: str,
        *,
        items_total: int,
        items_settled: int,
        items_failed: int,
        status: str,
        at: dt.datetime | None = None,
    ) -> ReconciliationRun:
        """Close a run with its counts.

        ``status`` is ``succeeded`` only when nothing failed. A pass that left thirteen
        items retryable is a ``failed`` run over a ``partially_reconciled`` batch, and
        both of those are ordinary Tuesday states — the sweep will pick the batch up
        again in fifteen minutes.
        """
        run = await self.get_or_raise(session, run_id)
        run.items_total = items_total
        run.items_settled = items_settled
        run.items_failed = items_failed
        run.status = status
        run.error_summary = error_summary
        run.finished_at = at or dt.datetime.now(dt.UTC)
        await session.flush()
        return run

    async def find_active(
        self, session: AsyncSession, *, batch_id: str
    ) -> ReconciliationRun | None:
        """The ``running`` run for a batch, if there is one.

        Uses ``pix_reconciliation_run_active``. Note what this is **not**: it is not a
        lock and it must never be used as one. Reading "no active run" and then starting
        work is check-then-act, which ADR 0011 forbids in the money path — take
        ``AdvisoryLockManager.try_acquire_batch_lock`` instead and let Postgres arbitrate.
        """
        stmt = (
            select(ReconciliationRun)
            .where(ReconciliationRun.batch_id == batch_id)
            .where(ReconciliationRun.status == "running")
            .order_by(ReconciliationRun.started_at.desc())
        )
        return (await session.execute(stmt)).scalars().first()

    async def list_running_batch_ids(
        self, session: AsyncSession, batch_ids: list[str] | tuple[str, ...]
    ) -> list[str]:
        """Which of ``batch_ids`` currently have a run in flight.

        The backlog service uses this to explain *why* a bucket is not draining. It would
        also be the input to PAY-2057 — skipping those batches up front instead of
        discovering the lock one failed retry at a time — which is still open.
        """
        if not batch_ids:
            return []
        stmt = (
            select(ReconciliationRun.batch_id)
            .where(ReconciliationRun.batch_id.in_(tuple(batch_ids)))
            .where(ReconciliationRun.status == "running")
            .distinct()
        )
        return [row[0] for row in (await session.execute(stmt)).all()]

    async def list_recent(
        self,
        session: AsyncSession,
        *,
        batch_id: str | None = None,
        limit: int = 20,
    ) -> list[ReconciliationRun]:
        """Recent runs, newest first, optionally for one batch.

        Backs ``GET /internal/v1/reconciliation/runs``. Uses
        ``ix_reconciliation_run_batch_started``.
        """
        stmt = select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc())
        if batch_id is not None:
            stmt = stmt.where(ReconciliationRun.batch_id == batch_id)
        stmt = stmt.limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def list_stuck(
        self, session: AsyncSession, *, older_than: dt.datetime
    ) -> list[ReconciliationRun]:
        """Runs still ``running`` since before ``older_than``.

        A sweep that is killed mid-pass — an ECS task rotation, an OOM — leaves its run
        open forever, because the row is only closed on the way out. This read is what the
        ops CLI's ``runs --stuck`` command prints, and it is how an operator tells a
        genuinely long pass from a corpse.
        """
        stmt = (
            select(ReconciliationRun)
            .where(ReconciliationRun.status == "running")
            .where(ReconciliationRun.started_at < older_than)
            .order_by(ReconciliationRun.started_at)
        )
        return list((await session.execute(stmt)).scalars().all())
