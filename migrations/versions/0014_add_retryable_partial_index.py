"""add retryable partial index

arc PERF. PAY-1502: the retry drain was sequential-scanning 2.4M ``reconciliation_item``
rows every sixty seconds to find a few hundred retryable ones. p99 on
``RetryScheduler.drain`` was **12 seconds**; after this index it is **40ms**.

Partial on ``status IN ('pending','retryable')``, because settled rows are 99.4% of the
table after four months and they have no business being in the index at all. The index is
a twentieth of the size of the equivalent full one and it stays that way as the table
grows, which is the entire argument in
`docs/adr/0010-partial-indexes-on-hot-paths.md`.

``CONCURRENTLY`` because ``reconciliation_item`` is written continuously and a plain
``CREATE INDEX`` takes an ``ACCESS EXCLUSIVE`` lock for the duration — on a 2.4M-row table
that is about ninety seconds of every settlement blocking.

Keyed on ``created_at`` today because that is the only ordering column the table has;
``0016`` re-points it at ``last_attempt_at`` when that column exists, and ``0024`` re-keys
it onto ``next_attempt_at``, which is when it stops merely being fast and starts
respecting the backoff.

Revision ID: 0014
Revises: 0013
Create Date: month 7 — mregression
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CONCURRENTLY cannot run inside a transaction block; Alembic wraps each migration in
    # one, so this migration and only this migration commits first.
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_reconciliation_item_retryable
            ON reconciliation_item (batch_id, created_at)
         WHERE status IN ('pending', 'retryable')
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_reconciliation_item_retryable")
