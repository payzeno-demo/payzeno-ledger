"""add reconciliation item attempt tracking

``attempt_count``, ``last_error_code``, ``last_attempt_at``.

Before this, a retryable item was retried forever: nothing counted the attempts, so
nothing could stop. An acquirer reference that will never match — a line for a charge that
belongs to a different Payzeno environment, which happens after every sandbox mishap —
sat in the drain's candidate list until somebody noticed the log.

``attempt_count >= 6`` is the ceiling, read from ``Settings.reconcile_max_attempts`` and
not from the constant, so it can be changed without a deploy. ``constants.MAX_ATTEMPTS``
is only the default.

Also re-points ``pix_reconciliation_item_retryable`` at ``last_attempt_at``, which is the
column the drain actually orders by now. Note what it still does **not** do: filter on it.
Ordering by the last attempt without excluding items attempted seconds ago is what lets a
drain re-pull the same failing item as fast as it can loop, and that is not fixed until
``0024``.

Revision ID: 0016
Revises: 0015
Create Date: month 8 — mregression
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reconciliation_item",
        sa.Column("attempt_count", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reconciliation_item", sa.Column("last_error_code", sa.Text(), nullable=True)
    )
    op.add_column(
        "reconciliation_item",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_reconciliation_item_retryable")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_reconciliation_item_retryable
            ON reconciliation_item (batch_id, last_attempt_at)
         WHERE status IN ('pending', 'retryable')
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_reconciliation_item_retryable")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_reconciliation_item_retryable
            ON reconciliation_item (batch_id, created_at)
         WHERE status IN ('pending', 'retryable')
        """
    )
    op.drop_column("reconciliation_item", "last_attempt_at")
    op.drop_column("reconciliation_item", "last_error_code")
    op.drop_column("reconciliation_item", "attempt_count")
