"""add settlement batch

One batch = one acquirer settlement file = one currency = one calendar processing day.

``uq_settlement_batch_file (acquirer, file_reference)`` is what makes importing the same
file twice a no-op instead of a duplicated day of money. Both acquirers re-file a
reference after a partial upload, so this is not theoretical.

``partially_reconciled`` is in the status enum from the start. It is the state that keeps a
retry backlog alive and it is entirely normal — items land in ``retryable`` for ordinary
acquirer 504s.

Revision ID: 0005
Revises: 0004
Create Date: month 2 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

BATCH_STATUSES = (
    "open",
    "closed",
    "reconciling",
    "reconciled",
    "partially_reconciled",
    "failed",
)


def upgrade() -> None:
    acquirer = sa.Enum("worldflow", "nordpay", name="acquirer")
    batch_status = sa.Enum(*BATCH_STATUSES, name="settlement_batch_status")

    op.create_table(
        "settlement_batch",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        # A genuine `date`: it is the acquirer's processing day, not an instant.
        sa.Column("processing_date", sa.Date(), nullable=False),
        sa.Column("acquirer", acquirer, nullable=False),
        sa.Column("file_reference", sa.Text(), nullable=False),
        sa.Column(
            "expected_total_minor", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "posted_total_minor", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", batch_status, nullable=False, server_default="open"),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_index(
        "uq_settlement_batch_file",
        "settlement_batch",
        ["acquirer", "file_reference"],
        unique=True,
    )
    op.execute(
        """
        CREATE INDEX ix_settlement_batch_status_date
            ON settlement_batch (status, processing_date DESC)
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_settlement_batch_updated_at
        BEFORE UPDATE ON settlement_batch
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_settlement_batch_updated_at ON settlement_batch")
    op.execute("DROP INDEX IF EXISTS ix_settlement_batch_status_date")
    op.drop_index("uq_settlement_batch_file", table_name="settlement_batch")
    op.drop_table("settlement_batch")
    sa.Enum(name="settlement_batch_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="acquirer").drop(op.get_bind(), checkfirst=True)
