"""add bank account projection

Fed by ``merchant.bank_account_verified``.

Without it, ``payout.bank_account_id`` is an id this service cannot resolve: ``bank_account``
is owned by payzeno-api, ``PayoutInitiator.initiate`` has to produce an ACH/SEPA/FPS
instruction, and there is no ledger→api route on the payout path. Up to now the rails read
the account details out of the create-payout request and trusted the caller, which is a
sentence that should not have survived review and did.

``CreatePayoutRequest.bank_account_id`` is also optional, so the ledger has to be able to
find the merchant's default account **for that currency** —
``pix_bank_account_projection_default``, unique and partial on ``is_default``, is what makes
"the default" a single row rather than a coin flip.

arc PCI: this table stores a vault token and last-four fragments. It never stores an account
number, and it has never had a column that could hold one.

Revision ID: 0028
Revises: 0027
Create Date: month 11 — mhandover
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_account_projection",
        sa.Column("bank_account_id", sa.Text(), primary_key=True),
        sa.Column("merchant_id", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("country", sa.CHAR(2), nullable=False),
        # aba | iban | uk_sort_code. Plain text rather than an enum: payzeno-api owns the
        # vocabulary and adding a rail should not need a migration on both sides.
        sa.Column("scheme", sa.Text(), nullable=False),
        sa.Column("account_number_token", sa.Text(), nullable=False),
        sa.Column("routing_last_four", sa.CHAR(4), nullable=True),
        sa.Column("iban_last_four", sa.CHAR(4), nullable=True),
        sa.Column("bic", sa.String(11), nullable=True),
        sa.Column("sort_code_last_four", sa.CHAR(4), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index(
        "ix_bank_account_projection_merchant",
        "bank_account_projection",
        ["merchant_id", "currency"],
    )
    op.execute(
        """
        CREATE UNIQUE INDEX pix_bank_account_projection_default
            ON bank_account_projection (merchant_id, currency, livemode)
         WHERE is_default
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pix_bank_account_projection_default")
    op.drop_index(
        "ix_bank_account_projection_merchant", table_name="bank_account_projection"
    )
    op.drop_table("bank_account_projection")
