"""add settlement batch currency+date index

arc PERF, the tail end. The finance export runs "every EUR batch for the quarter" and was
using ``ix_settlement_batch_status_date``, which is keyed on status first — so it read
every reconciled batch in every currency and threw away four fifths of them.

Small table by the standards of this database (a few thousand rows a year), so this is a
seconds-long index build and does not need ``CONCURRENTLY``. It is here because the export
runs inside a lambda with a thirty-second timeout and it had started failing.

Revision ID: 0021
Revises: 0020
Create Date: month 10 — mregression
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_settlement_batch_currency_date
            ON settlement_batch (currency, processing_date DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_settlement_batch_currency_date")
