"""add reconciliation run

One row per pass over one batch. Bookkeeping, not control flow — nothing waits on a run
and nothing locks on one. Mutual exclusion between two sweeps is the batch advisory lock
in ``app/db/locks.py``.

``pix_reconciliation_run_active`` is partial on ``status = 'running'`` so the ops console
can answer "is something working this batch right now" without a scan. It is a display
index. Reading it and then deciding to start work would be check-then-act, which is the
one thing this subsystem must not do.

Revision ID: 0008
Revises: 0007
Create Date: month 4 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    trigger = sa.Enum("scheduled", "manual", "retry", name="reconciliation_trigger")
    run_status = sa.Enum(
        "running", "succeeded", "failed", name="reconciliation_run_status"
    )

    op.create_table(
        "reconciliation_run",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Text(),
            sa.ForeignKey("settlement_batch.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trigger", trigger, nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column("items_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_settled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.execute(
        """
        CREATE INDEX ix_reconciliation_run_batch_started
            ON reconciliation_run (batch_id, started_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX pix_reconciliation_run_active
            ON reconciliation_run (batch_id)
         WHERE status = 'running'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pix_reconciliation_run_active")
    op.execute("DROP INDEX IF EXISTS ix_reconciliation_run_batch_started")
    op.drop_table("reconciliation_run")
    sa.Enum(name="reconciliation_run_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="reconciliation_trigger").drop(op.get_bind(), checkfirst=True)
