"""add ledger adjustment request

Maker-checker for the one path that lets a human move money by hand.

``adjustment`` has been a valid ``ledger_purpose`` since ``0001`` and ``created_by='admin'``
has been valid since ``0002``, which together mean a person with database access — or an
ops endpoint with a bug — can post arbitrary entries against merchant money with no second
pair of eyes, no reason code and no approval record. ``payzeno_ledger`` has no ``audit_log``
table of its own to fall back on either. This table is that record.

From here, ``AdjustmentPostingRule`` is reachable **only** through an ``approved`` row.

``chk_adjustment_dual_control`` enforces ``approved_by <> requested_by`` at the storage
layer. ``AdjustmentService.approve`` raises ``DualControlRequiredError`` before it gets
there; the constraint is what makes the promise true for a psql session as well.

Revision ID: 0030
Revises: 0029
Create Date: month 11 — dhotfix
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status = sa.Enum(
        "pending", "approved", "rejected", "posted", name="ledger_adjustment_status"
    )

    op.create_table(
        "ledger_adjustment_request",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("merchant_id", sa.Text(), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        # The proposed postings, as they were requested. Stored verbatim so the approver
        # reviews what was asked for and not a re-rendering of it.
        sa.Column(
            "lines", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "posted_transaction_id",
            sa.Text(),
            sa.ForeignKey("ledger_transaction.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", status, nullable=False, server_default="pending"),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> requested_by",
            name="chk_adjustment_dual_control",
        ),
    )

    op.execute(
        """
        CREATE INDEX pix_ledger_adjustment_pending
            ON ledger_adjustment_request (requested_at)
         WHERE status = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_ledger_adjustment_merchant
            ON ledger_adjustment_request (merchant_id, requested_at DESC)
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_adjustment_updated_at
        BEFORE UPDATE ON ledger_adjustment_request
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ledger_adjustment_updated_at ON ledger_adjustment_request"
    )
    op.execute("DROP INDEX IF EXISTS ix_ledger_adjustment_merchant")
    op.execute("DROP INDEX IF EXISTS pix_ledger_adjustment_pending")
    op.drop_table("ledger_adjustment_request")
    sa.Enum(name="ledger_adjustment_status").drop(op.get_bind(), checkfirst=True)
