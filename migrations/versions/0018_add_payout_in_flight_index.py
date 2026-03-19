"""add payout in-flight partial index

``PayoutCalculator.compute_available`` subtracts in-flight payouts on every payout request,
and the query was scanning ``payout`` by merchant and filtering the status in memory. For
the ten largest merchants that is tens of thousands of rows to find at most one.

Partial on ``status IN ('scheduled','in_transit')`` — the in-flight set is tiny and the
terminal set grows forever.

**Not unique yet.** It should be: at most one payout per merchant per currency can be in
flight at a time, and the calculator's read-then-insert is the same check-then-act shape
that will make PAY-2041 famous next month, on the path that moves money to a bank
account. Making it unique needs a dedupe pass over live data first, and that is ``0033``.

Revision ID: 0018
Revises: 0017
Create Date: month 8 — mhandover
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_payout_in_flight
            ON payout (merchant_id, currency)
         WHERE status IN ('scheduled', 'in_transit')
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_payout_in_flight")
