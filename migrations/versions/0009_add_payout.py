"""add payout

Money leaving for a merchant's bank account.

``chk_payout_reversal_present`` is here from the start and it is the important line: a
payout in ``failed`` or ``returned`` must carry a ``reversal_transaction_id``. Without it,
an ACH failure permanently destroys the merchant's money — the ``payout`` posting already
debited ``merchant_payable``, ``failed`` is terminal, and nothing gives it back.

``available_on`` is a genuine ``date``: it is a banking-calendar day, not an instant, and
it comes from ``BankingCalendar.next_business_day`` rather than from ledger booking time.

Revision ID: 0009
Revises: 0008
Create Date: month 4 — mhandover
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

PAYOUT_STATUSES = (
    "scheduled",
    "in_transit",
    "paid",
    "failed",
    "returned",
    "canceled",
)

PAYOUT_METHODS = ("standard_ach", "same_day_ach", "sepa", "faster_payments", "debit_ach")

FAILURE_CODES = (
    "account_closed",
    "no_account",
    "invalid_details",
    "debit_not_authorized",
    "insufficient_funds",
    "bank_rejected",
)


def upgrade() -> None:
    status = sa.Enum(*PAYOUT_STATUSES, name="payout_status")
    method = sa.Enum(*PAYOUT_METHODS, name="payout_method")
    failure_code = sa.Enum(*FAILURE_CODES, name="payout_failure_code")

    op.create_table(
        "payout",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("merchant_id", sa.Text(), nullable=False),
        # The payzeno-api bank account id. Resolved through bank_account_projection
        # from 0028; before that the rails read it out of the request and hoped.
        sa.Column("bank_account_id", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", status, nullable=False, server_default="scheduled"),
        sa.Column("method", method, nullable=False),
        sa.Column("available_on", sa.Date(), nullable=False),
        sa.Column("arrival_estimate", sa.Date(), nullable=True),
        sa.Column("statement_descriptor", sa.String(22), nullable=False),
        sa.Column("failure_code", failure_code, nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "ledger_transaction_id",
            sa.Text(),
            sa.ForeignKey("ledger_transaction.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "reversal_transaction_id",
            sa.Text(),
            sa.ForeignKey("ledger_transaction.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("bank_reference", sa.Text(), nullable=True),
        sa.Column("initiated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount_minor > 0", name="chk_payout_amount_positive"),
        sa.CheckConstraint(
            "status NOT IN ('failed','returned') OR reversal_transaction_id IS NOT NULL",
            name="chk_payout_reversal_present",
        ),
    )

    op.execute(
        """
        CREATE INDEX ix_payout_merchant_created
            ON payout (merchant_id, created_at DESC)
        """
    )
    op.create_index("ix_payout_status_available", "payout", ["status", "available_on"])
    op.execute(
        """
        CREATE TRIGGER trg_payout_updated_at
        BEFORE UPDATE ON payout
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_payout_updated_at ON payout")
    op.drop_index("ix_payout_status_available", table_name="payout")
    op.execute("DROP INDEX IF EXISTS ix_payout_merchant_created")
    op.drop_table("payout")
    sa.Enum(name="payout_failure_code").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="payout_method").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="payout_status").drop(op.get_bind(), checkfirst=True)
