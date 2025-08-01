"""init accounts

The chart of accounts, the three enums it needs, and ``set_updated_at()`` — the trigger
function every mutable table in this database hangs off. Nothing before this exists.

``uq_account_merchant_type_currency`` cannot cover platform accounts: they carry a null
``merchant_id`` and a unique index treats every null as distinct, so twelve ``cash`` rows
would be perfectly legal. Hence the partial-unique ``pix_account_platform`` alongside it.
Both are widened by ``0032`` when ``livemode`` arrives.

Revision ID: 0001
Revises:
Create Date: month 1 — dhotfix
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ACCOUNT_TYPES = (
    "processor_clearing",
    "merchant_receivable",
    "authorization_hold",
    "merchant_payable",
    "platform_fee_revenue",
    "interchange_expense",
    "scheme_fee_expense",
    "acquirer_fee_expense",
    "reserve",
    "chargeback_liability",
    "cash",
    "rounding_adjustment",
)


def upgrade() -> None:
    # Maintained by a trigger and not by the ORM: the ops CLI and the data migrations in
    # this directory write rows through raw SQL and must not be able to skip it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    account_type = sa.Enum(*ACCOUNT_TYPES, name="account_type")
    entry_direction = sa.Enum("debit", "credit", name="entry_direction")
    account_status = sa.Enum("active", "frozen", "closed", name="account_status")

    op.create_table(
        "account",
        sa.Column("id", sa.Text(), primary_key=True),
        # Null for platform-level accounts. There is no FK: `merchant` lives in
        # payzeno_api and the two databases are never joined.
        sa.Column("merchant_id", sa.Text(), nullable=True),
        sa.Column("type", account_type, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("normal_balance", entry_direction, nullable=False),
        sa.Column(
            "status", account_status, nullable=False, server_default="active"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_index(
        "uq_account_merchant_type_currency",
        "account",
        ["merchant_id", "type", "currency"],
        unique=True,
    )
    op.execute(
        """
        CREATE UNIQUE INDEX pix_account_platform
            ON account (type, currency)
         WHERE merchant_id IS NULL
        """
    )
    op.create_index("ix_account_merchant_id", "account", ["merchant_id"])

    op.execute(
        """
        CREATE TRIGGER trg_account_updated_at
        BEFORE UPDATE ON account
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_account_updated_at ON account")
    op.drop_index("ix_account_merchant_id", table_name="account")
    op.execute("DROP INDEX IF EXISTS pix_account_platform")
    op.drop_index("uq_account_merchant_type_currency", table_name="account")
    op.drop_table("account")
    sa.Enum(name="account_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="entry_direction").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="account_type").drop(op.get_bind(), checkfirst=True)
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
