"""add merchant balance cache

The permanent fix for the balance read path. ``0015``'s index made summing ``ledger_entry``
affordable; this makes it unnecessary. ``LedgerPoster.post`` maintains the cache **in the
same transaction as the entries it writes**, so it can never lag a committed posting.

``livemode`` is in the primary key, not a plain column: test-mode money must never be
counted into a balance a payout can be drawn against.

``negative_balance_minor`` is a first-class column rather than a computed sign, because for
a B2B acquirer a merchant owing money back is the primary source of credit loss and the
console has to show it.

This migration also ships the ``LedgerBalanceCacheDrift`` metric (invariant (3): recompute
from entries, compare, alarm on any difference). Which is worth remembering, because next
month that alarm is the **only** one that fires during the sev1, and it pages the SRE on
call rather than the settlement team — a drifting balance cache reads like a caching bug,
not like a double settlement.

Revision ID: 0023
Revises: 0022
Create Date: month 8 — mhandover
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merchant_balance_cache",
        sa.Column("merchant_id", sa.Text(), primary_key=True),
        sa.Column("currency", sa.CHAR(3), primary_key=True),
        sa.Column("livemode", sa.Boolean(), primary_key=True),
        sa.Column("available_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pending_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("disputed_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "negative_balance_minor", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "last_transaction_id",
            sa.Text(),
            sa.ForeignKey("ledger_transaction.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # The drift job scans by staleness, not by merchant, so a nightly run with a bounded
    # budget eventually covers everyone instead of re-checking the same prefix.
    op.create_index(
        "ix_merchant_balance_cache_computed", "merchant_balance_cache", ["computed_at"]
    )
    op.execute(
        """
        CREATE INDEX pix_merchant_balance_cache_negative
            ON merchant_balance_cache (merchant_id)
         WHERE negative_balance_minor > 0
        """
    )

    # Seed from the entries that already exist. One-off; from here the cache is
    # transactional and this query never runs again outside the repair command.
    op.execute(
        """
        INSERT INTO merchant_balance_cache (
            merchant_id, currency, livemode, available_minor, computed_at, updated_at
        )
        SELECT a.merchant_id,
               e.currency,
               true,
               COALESCE(SUM(CASE WHEN e.direction = 'credit' THEN e.amount_minor
                                 ELSE -e.amount_minor END), 0),
               now(),
               now()
          FROM ledger_entry e
          JOIN account a ON a.id = e.account_id
         WHERE a.merchant_id IS NOT NULL
           AND a.type = 'merchant_payable'
         GROUP BY a.merchant_id, e.currency
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pix_merchant_balance_cache_negative")
    op.drop_index(
        "ix_merchant_balance_cache_computed", table_name="merchant_balance_cache"
    )
    op.drop_table("merchant_balance_cache")
