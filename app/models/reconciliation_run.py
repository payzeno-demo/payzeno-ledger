"""``reconciliation_run`` — one execution of the reconciliation pass over one batch.

``pix_reconciliation_run_active`` is what PAY-2057 wants: instead of the retry drain
discovering a running sweep lock by lock (200 failed advisory acquisitions per pass), it
should ask this index whether the item's batch already has a ``running`` run and skip the
whole batch. The ticket is still open — see the TODO in
``app/services/reconciliation/backlog.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    CreatedAtMixin,
    reconciliation_run_status_enum,
    reconciliation_trigger_enum,
)


class ReconciliationRun(Base, CreatedAtMixin):
    """One pass of ``ReconciliationService.reconcile_batch`` over one batch."""

    __tablename__ = "reconciliation_run"
    entity_name: ClassVar[str] = "reconciliation_run"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        Text, ForeignKey("settlement_batch.id", ondelete="RESTRICT"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(reconciliation_trigger_enum, nullable=False)
    status: Mapped[str] = mapped_column(reconciliation_run_status_enum, nullable=False)

    #: NOT NULL with a downstream consumer in `settlement.completed`. The counters are
    #: passed explicitly to `ReconciliationRunRepository.finish` because the run is
    #: loaded in one session and the item loop runs in many others — mutating
    #: `run.items_settled` across them mutates a detached instance.
    items_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    items_settled: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_reconciliation_run_batch_started",
            "batch_id",
            "started_at",
            postgresql_ops={"started_at": "DESC"},
        ),
        # One running run per batch. Not unique: a crashed task leaves a `running` row
        # behind and a unique index would then block the batch forever with no operator
        # path to clear it short of a manual UPDATE.
        Index(
            "pix_reconciliation_run_active",
            "batch_id",
            postgresql_where="status = 'running'",
        ),
    )

    def is_running(self) -> bool:
        """Whether this run still holds the batch."""
        return self.status == "running"

    def duration_ms(self) -> int | None:
        """Wall time of the pass, or ``None`` while it is still running."""
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def outcome_status(self) -> str:
        """``succeeded`` when nothing failed, else ``failed``.

        ``reconcile_batch`` computes this from its local counters and passes it to
        ``finish``; this method is what the ops CLI and ``LedgerAuditService`` use to
        re-derive it from a stored row.
        """
        return "succeeded" if self.items_failed == 0 else "failed"
