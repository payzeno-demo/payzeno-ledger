"""add settlement charge projection

The ledger's copy of a payzeno-api charge, written from ``payment.authorized``. It is the
right-hand side of every reconciliation match.

``reserve_bps``, ``platform_fee_bps`` and ``platform_fee_fixed_minor`` are **denormalised
at authorisation time on purpose**: a merchant who renegotiates their rate on Thursday must
not retroactively change what an already-authorised Tuesday charge settles at.

``ix_settlement_charge_processor_reference (acquirer, processor_reference)`` is the primary
match key. It is scoped by acquirer because the two of them mint references in their own
namespaces and a bare reference string collides across them roughly as often as you would
expect from six-character prefixes.

Revision ID: 0011
Revises: 0010
Create Date: month 5 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settlement_charge",
        sa.Column("charge_id", sa.Text(), primary_key=True),
        sa.Column("merchant_id", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column(
            "acquirer", postgresql.ENUM(name="acquirer", create_type=False), nullable=False
        ),
        sa.Column("network_transaction_id", sa.Text(), nullable=True),
        sa.Column("processor_reference", sa.Text(), nullable=True),
        sa.Column("capture_method", sa.Text(), nullable=False, server_default="automatic"),
        sa.Column("reserve_bps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("platform_fee_bps", sa.Integer(), nullable=False, server_default="290"),
        sa.Column(
            "platform_fee_fixed_minor", sa.BigInteger(), nullable=False, server_default="30"
        ),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index("ix_settlement_charge_merchant", "settlement_charge", ["merchant_id"])
    op.create_index(
        "uq_settlement_charge_network_txn",
        "settlement_charge",
        ["acquirer", "network_transaction_id"],
        unique=True,
    )
    op.create_index(
        "ix_settlement_charge_processor_reference",
        "settlement_charge",
        ["acquirer", "processor_reference"],
    )
    # Backs the heuristic amount-window match: same merchant, same currency, near amount,
    # authorised within 48h. Non-unique by definition — if it were unique the strategy
    # would not need to abstain on multiple candidates.
    op.execute(
        """
        CREATE INDEX ix_settlement_charge_heuristic
            ON settlement_charge (merchant_id, currency, amount_minor, authorized_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_settlement_charge_heuristic")
    op.drop_index(
        "ix_settlement_charge_processor_reference", table_name="settlement_charge"
    )
    op.drop_index("uq_settlement_charge_network_txn", table_name="settlement_charge")
    op.drop_index("ix_settlement_charge_merchant", table_name="settlement_charge")
    op.drop_table("settlement_charge")
