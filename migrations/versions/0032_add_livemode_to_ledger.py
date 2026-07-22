"""add livemode across the ledger

``livemode`` on ``account``, ``ledger_transaction``, ``ledger_entry``, ``settlement_batch``,
``reconciliation_item``, ``payout`` and both projections, and widened into
``uq_account_merchant_type_currency`` and ``pix_payout_in_flight``.

Test-mode money must never touch a live account. Until now the separation was by
convention — test merchants had test-looking ids — and that convention had already failed
once, in a sandbox settlement file that reconciled against live charges because the
acquirer reference happened to match.

**Two-phase, as review requires.** This release adds the column nullable, backfills ``true``
from the merchant projection, and leaves it nullable. ``0032b`` — next release — sets it
``NOT NULL``. A single-phase add would mean the running fleet, which does not yet write the
column, starts failing every insert the moment the migration lands.

Invariant (9) follows from it: no transaction may have entries spanning two ``livemode``
values, checked nightly and enforced by ``LedgerPoster`` raising
``LivemodeMismatchError``.

Revision ID: 0032
Revises: 0031
Create Date: month 12 — dhotfix
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

#: Tables that gain the column here. The projections already have it — they were written
#: after the convention existed — which is why they are not in this list.
TABLES = (
    "account",
    "ledger_transaction",
    "ledger_entry",
    "settlement_batch",
    "reconciliation_item",
)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("livemode", sa.Boolean(), nullable=True))

    # Everything that exists today is live: the sandbox writes to a separate database and
    # always has. The backfill is therefore a constant and not a join, which matters —
    # ledger_entry is 40M rows and a correlated update would not finish inside a deploy.
    for table in TABLES:
        op.execute(f"UPDATE {table} SET livemode = true WHERE livemode IS NULL")  # noqa: S608

    # payout already carried it from 0009; the projections from their own migrations.
    # Widen the two unique indexes that now have to include it.
    op.drop_index("uq_account_merchant_type_currency", table_name="account")
    op.create_index(
        "uq_account_merchant_type_currency_livemode",
        "account",
        ["merchant_id", "type", "currency", "livemode"],
        unique=True,
    )
    op.execute("DROP INDEX IF EXISTS pix_account_platform")
    op.execute(
        """
        CREATE UNIQUE INDEX pix_account_platform
            ON account (type, currency, livemode)
         WHERE merchant_id IS NULL
        """
    )

    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_payout_in_flight")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_payout_in_flight
            ON payout (merchant_id, currency, livemode)
         WHERE status IN ('scheduled', 'in_transit')
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_payout_in_flight")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_payout_in_flight
            ON payout (merchant_id, currency)
         WHERE status IN ('scheduled', 'in_transit')
        """
    )
    op.execute("DROP INDEX IF EXISTS pix_account_platform")
    op.execute(
        """
        CREATE UNIQUE INDEX pix_account_platform
            ON account (type, currency)
         WHERE merchant_id IS NULL
        """
    )
    op.drop_index("uq_account_merchant_type_currency_livemode", table_name="account")
    op.create_index(
        "uq_account_merchant_type_currency",
        "account",
        ["merchant_id", "type", "currency"],
        unique=True,
    )
    for table in reversed(TABLES):
        op.drop_column(table, "livemode")
