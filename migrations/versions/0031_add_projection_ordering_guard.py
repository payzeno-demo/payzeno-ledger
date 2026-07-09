"""add source_occurred_at to every projection

Every projection write becomes a **conditional** upsert:

``... ON CONFLICT DO UPDATE ... WHERE <table>.source_occurred_at < EXCLUDED.source_occurred_at``

Ordering is not guaranteed on the bus and ``source_event_id`` is a ULID of the *event*, not
a monotonic token for the entity — so "last write wins" means "whichever redelivery arrived
last wins". Concretely, without this guard:

* a redelivered ``merchant.status_changed`` silently un-restricts a merchant that risk
  suspended ten minutes ago;
* a redelivered ``payment.authorized`` arriving after ``payment.captured`` clears
  ``captured_at``;
* a stale ``merchant.updated`` re-enables ``capture_at_settlement`` in the ledger's view of
  a charge that has already settled.

The third one is why this is a month-12 ticket and not a month-18 one.

Backfilled from ``updated_at``, which is the closest thing to the truth that exists at this
point and is monotonic per row.

Revision ID: 0031
Revises: 0030
Create Date: month 12 — mregression
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

PROJECTION_TABLES = (
    "merchant_projection",
    "settlement_charge",
    "bank_account_projection",
)


def upgrade() -> None:
    for table in PROJECTION_TABLES:
        # Two-phase in miniature: add nullable, backfill, then set NOT NULL. A NOT NULL
        # add with a default would rewrite the table under an ACCESS EXCLUSIVE lock, and
        # settlement_charge is the second-largest table in the database.
        op.add_column(
            table,
            sa.Column("source_occurred_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.execute(
            f"UPDATE {table} SET source_occurred_at = updated_at "  # noqa: S608
            "WHERE source_occurred_at IS NULL"
        )
        op.alter_column(table, "source_occurred_at", nullable=False)

    # The drift job wants to know when a projection last moved, independent of when the
    # row was written.
    op.execute(
        """
        CREATE INDEX ix_merchant_projection_occurred
            ON merchant_projection (source_occurred_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_merchant_projection_occurred")
    for table in PROJECTION_TABLES:
        op.drop_column(table, "source_occurred_at")
