"""add reserve hold, banking calendar and four ledger purposes

**Reserve is released, not accumulated.** ``capture`` has been crediting the ``reserve``
account for merchants on a rolling reserve since month 1, and nothing has ever given it
back: a merchant on 10% has been accruing money Payzeno could not pay out. ``reserve_hold``
records each withholding with a ``release_on`` date and ``ReserveReleaseJob`` posts
``reserve_release`` when it arrives.

**Banking calendar.** ``available_on`` was ``settlement_date + payout_delay_days``, which
pays merchants on bank holidays and then reverses. The calendar is keyed
``(currency, rail, calendar_date)`` because SEPA, Faster Payments and the two ACH rails are
in different jurisdictions and share neither holidays nor cutoffs — which is also why
there is no single ``PAYOUT_CUTOFF_HOUR_UTC`` in the config and four per-rail values
instead.

**Four purposes**: ``auth_release`` (the hold has always been released inline by
``capture`` and never had its own name), ``settlement_funding`` (``0027``'s bank side),
``reserve_release`` and ``payout_reversal``. Added together because every
``ALTER TYPE ... ADD VALUE`` needs its own committed statement and doing them one release
at a time would take four.

Revision ID: 0029
Revises: 0028
Create Date: month 11 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

NEW_PURPOSES = (
    "auth_release",
    "settlement_funding",
    "reserve_release",
    "payout_reversal",
)


def upgrade() -> None:
    op.execute("COMMIT")
    for purpose in NEW_PURPOSES:
        op.execute(f"ALTER TYPE ledger_purpose ADD VALUE IF NOT EXISTS '{purpose}'")

    op.execute(
        "ALTER TYPE ledger_reference_type ADD VALUE IF NOT EXISTS 'funding_event'"
    )
    op.execute("ALTER TYPE ledger_reference_type ADD VALUE IF NOT EXISTS 'reserve_hold'")

    op.create_table(
        "reserve_hold",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("merchant_id", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "held_from_transaction_id",
            sa.Text(),
            sa.ForeignKey("ledger_transaction.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # capture_date + merchant.reserve_hold_days. A banking date, not an instant.
        sa.Column("release_on", sa.Date(), nullable=False),
        sa.Column(
            "released_transaction_id",
            sa.Text(),
            sa.ForeignKey("ledger_transaction.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount_minor > 0", name="chk_reserve_hold_amount_positive"),
    )
    op.execute(
        """
        CREATE INDEX pix_reserve_hold_due
            ON reserve_hold (release_on)
         WHERE released_transaction_id IS NULL
        """
    )
    op.create_index(
        "ix_reserve_hold_merchant", "reserve_hold", ["merchant_id", "currency"]
    )

    op.create_table(
        "banking_calendar",
        sa.Column("currency", sa.CHAR(3), primary_key=True),
        sa.Column(
            "rail",
            postgresql.ENUM(name="payout_method", create_type=False),
            primary_key=True,
        ),
        sa.Column("calendar_date", sa.Date(), primary_key=True),
        sa.Column("is_business_day", sa.Boolean(), nullable=False),
        sa.Column("holiday_name", sa.Text(), nullable=True),
    )
    # Holidays are the interesting rows and they are ~2% of the table.
    op.execute(
        """
        CREATE INDEX pix_banking_calendar_holidays
            ON banking_calendar (currency, rail, calendar_date)
         WHERE is_business_day = false
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_reserve_hold_updated_at
        BEFORE UPDATE ON reserve_hold
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_reserve_hold_updated_at ON reserve_hold")
    op.execute("DROP INDEX IF EXISTS pix_banking_calendar_holidays")
    op.drop_table("banking_calendar")
    op.drop_index("ix_reserve_hold_merchant", table_name="reserve_hold")
    op.execute("DROP INDEX IF EXISTS pix_reserve_hold_due")
    op.drop_table("reserve_hold")
    # Enum values are not removable in PostgreSQL. They stay.
