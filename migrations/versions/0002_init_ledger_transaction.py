"""init ledger transaction

The transaction header. Entries come in ``0003``; a transaction with no entries is
meaningless but the tables have to arrive in FK order.

Note what is *not* here: ``idempotency_key``. It arrives in ``0007``, four weeks later,
and the shape of that migration is the whole of arc INC.

Revision ID: 0002
Revises: 0001
Create Date: month 1 — dhotfix
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# The month-1 set. `auth_release`, `settlement_funding`, `reserve_release` and
# `payout_reversal` are added by 0029, once the postings that need them exist.
LEDGER_PURPOSES = (
    "auth",
    "capture",
    "settle",
    "fee",
    "refund",
    "dispute",
    "payout",
    "reversal",
    "adjustment",
)

LEDGER_REFERENCE_TYPES = (
    "payment_intent",
    "charge",
    "refund",
    "dispute",
    "payout",
    "settlement_batch",
    "reconciliation_item",
)


def upgrade() -> None:
    ledger_purpose = sa.Enum(*LEDGER_PURPOSES, name="ledger_purpose")
    reference_type = sa.Enum(*LEDGER_REFERENCE_TYPES, name="ledger_reference_type")
    ledger_actor = sa.Enum(
        "system", "reconciliation", "payout_worker", "admin", name="ledger_actor"
    )

    op.create_table(
        "ledger_transaction",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("purpose", ledger_purpose, nullable=False),
        sa.Column("merchant_id", sa.Text(), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        # Polymorphic pointer at the payzeno-api object this posting is about. No FK —
        # different database.
        sa.Column("reference_type", reference_type, nullable=False),
        sa.Column("reference_id", sa.Text(), nullable=False),
        # Corrections are compensating transactions, never edits: `ledger_entry` is
        # append-only from 0004 onward.
        sa.Column(
            "reverses_transaction_id",
            sa.Text(),
            sa.ForeignKey("ledger_transaction.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "created_by", ledger_actor, nullable=False, server_default="system"
        ),
        sa.Column(
            "posted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_index(
        "ix_ledger_transaction_reference",
        "ledger_transaction",
        ["reference_type", "reference_id"],
    )
    op.execute(
        """
        CREATE INDEX ix_ledger_transaction_merchant_posted
            ON ledger_transaction (merchant_id, posted_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_ledger_transaction_purpose_posted
            ON ledger_transaction (purpose, posted_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ledger_transaction_purpose_posted")
    op.execute("DROP INDEX IF EXISTS ix_ledger_transaction_merchant_posted")
    op.drop_index("ix_ledger_transaction_reference", table_name="ledger_transaction")
    op.drop_table("ledger_transaction")
    sa.Enum(name="ledger_actor").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="ledger_reference_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="ledger_purpose").drop(op.get_bind(), checkfirst=True)
