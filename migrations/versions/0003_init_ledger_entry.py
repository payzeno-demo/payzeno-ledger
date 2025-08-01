"""init ledger entry

The leg table. ``amount_minor > 0`` is a check constraint and not a convention: direction
carries the sign, and a negative credit is the same thing as a positive debit written by
somebody who was not paying attention.

``uq_ledger_entry_txn_sequence`` stops a partially-retried insert producing a transaction
with three legs numbered 0, 1, 1.

Revision ID: 0003
Revises: 0002
Create Date: month 1 — dhotfix
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ledger_entry",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "transaction_id",
            sa.Text(),
            sa.ForeignKey("ledger_transaction.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Text(),
            sa.ForeignKey("account.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "direction",
            postgresql.ENUM(name="entry_direction", create_type=False),
            nullable=False,
        ),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        # 0-based ordinal within the transaction, so a trial-balance report prints the
        # legs in the order the posting rule built them.
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount_minor > 0", name="chk_ledger_entry_amount_positive"),
    )

    op.create_index(
        "uq_ledger_entry_txn_sequence",
        "ledger_entry",
        ["transaction_id", "sequence"],
        unique=True,
    )
    op.create_index("ix_ledger_entry_transaction_id", "ledger_entry", ["transaction_id"])


def downgrade() -> None:
    op.drop_index("ix_ledger_entry_transaction_id", table_name="ledger_entry")
    op.drop_index("uq_ledger_entry_txn_sequence", table_name="ledger_entry")
    op.drop_table("ledger_entry")
