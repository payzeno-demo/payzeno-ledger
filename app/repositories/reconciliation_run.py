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
        limit: int = 20,
    ) -> list[ReconciliationRun]:
        """Recent runs, newest first, optionally for one batch.

        Backs ``GET /internal/v1/reconciliation/runs``. Uses
        ``ix_reconciliation_run_batch_started``.
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
