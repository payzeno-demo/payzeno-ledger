"""add reconciliation item

One acquirer line item: the atom reconciliation settles.

``uq_reconciliation_item_acquirer_ref (batch_id, acquirer_reference)`` is scoped to the
batch, because acquirer references are only unique within a file.

There is **no unique index on ``charge_id``** and there must not be. A charge legitimately
appears in two batches — the original settlement and a chargeback representment — so a
unique index here would reject correct data. It is also, later, the reason a reviewer
looking at this table alone concludes a duplicate settlement is impossible.

Revision ID: 0006
Revises: 0005
Create Date: month 3 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

ITEM_STATUSES = (
    "pending",
    "settling",
    "settled",
    "retryable",
    "failed",
    "orphaned",
)


def upgrade() -> None:
    item_status = sa.Enum(*ITEM_STATUSES, name="reconciliation_item_status")

    op.create_table(
        "reconciliation_item",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Text(),
            sa.ForeignKey("settlement_batch.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Null while unmatched, and null forever on a line that is not a sale.
        sa.Column("charge_id", sa.Text(), nullable=True),
        sa.Column("merchant_id", sa.Text(), nullable=True),
        sa.Column("gross_minor", sa.BigInteger(), nullable=False),
        # What the acquirer kept. An expense, not revenue.
        sa.Column("fee_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("net_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("acquirer_reference", sa.Text(), nullable=False),
        sa.Column("status", item_status, nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_index(
        "uq_reconciliation_item_acquirer_ref",
        "reconciliation_item",
        ["batch_id", "acquirer_reference"],
        unique=True,
    )
    op.create_index(
        "ix_reconciliation_item_batch_status",
        "reconciliation_item",
        ["batch_id", "status"],
    )
    # Deliberately NOT unique. See the module docstring.
    op.create_index(
        "ix_reconciliation_item_charge_id", "reconciliation_item", ["charge_id"]
    )
    op.execute(
        """
        CREATE TRIGGER trg_reconciliation_item_updated_at
        BEFORE UPDATE ON reconciliation_item
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_reconciliation_item_updated_at ON reconciliation_item"
    )
    op.drop_index("ix_reconciliation_item_charge_id", table_name="reconciliation_item")
    op.drop_index("ix_reconciliation_item_batch_status", table_name="reconciliation_item")
    op.drop_index("uq_reconciliation_item_acquirer_ref", table_name="reconciliation_item")
    op.drop_table("reconciliation_item")
    sa.Enum(name="reconciliation_item_status").drop(op.get_bind(), checkfirst=True)
