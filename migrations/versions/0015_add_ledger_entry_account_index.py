"""add ledger entry account index

arc PERF, the second half. PAY-1508: ``GET /internal/v1/balances/{merchantId}`` was
summing ``ledger_entry`` with no usable index and taking 4.2 seconds at p99. The legacy
Java biller calls that endpoint **inside invoice generation**, once per merchant per
invoice, so the monthly billing run was spending most of its wall clock here.

``(account_id, created_at DESC)`` rather than ``(account_id)`` alone: every caller either
bounds the sum by ``as_of`` or wants the most recent entries, and the trailing sort was
what actually hurt.

The permanent fix for the request path is ``merchant_balance_cache`` (``0023``), which
serves the balance from one indexed row. This index is what makes the *audit* recompute
and the historical read affordable, and both still use it.

Revision ID: 0015
Revises: 0014
Create Date: month 7 — mregression
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ledger_entry_account_created
            ON ledger_entry (account_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_ledger_entry_account_created")
