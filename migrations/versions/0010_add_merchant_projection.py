"""add merchant projection

The ledger's read-optimised copy of a merchant's commercial terms, fed by
``merchant.created`` / ``merchant.updated`` / ``merchant.status_changed``.

It exists because reconciliation and payouts need a merchant's fee terms, tolerance and
status on the settlement path, and a synchronous call into payzeno-api for every one of
four thousand items is not a settlement path — it is an outage waiting for payzeno-api to
have a bad afternoon.

The projection is eventually consistent and never authoritative. ``source_occurred_at``
and the conditional upsert that reads it arrive in ``0031``; until then the last event to
land wins, which is wrong roughly as often as SQS redelivers.

Revision ID: 0010
Revises: 0009
Create Date: month 5 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merchant_projection",
        sa.Column("merchant_id", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("country", sa.CHAR(2), nullable=True),
        sa.Column("default_currency", sa.CHAR(3), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("risk_tier", sa.Text(), nullable=False, server_default="standard"),
        sa.Column("reserve_bps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserve_hold_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pricing_model", sa.Text(), nullable=False, server_default="blended"),
        sa.Column("platform_fee_bps", sa.Integer(), nullable=False, server_default="290"),
        sa.Column(
            "platform_fee_fixed_minor", sa.BigInteger(), nullable=False, server_default="30"
        ),
        sa.Column("payout_delay_days", sa.Integer(), nullable=False, server_default="2"),
        # Per-item variance tolerance. SettlementPoster reads it off this row and
        # refuses to post anything outside it.
        sa.Column(
            "settlement_tolerance_minor",
            sa.BigInteger(),
            nullable=False,
            server_default="100",
        ),
        sa.Column("payout_schedule", sa.Text(), nullable=False, server_default="daily"),
        # Which bus event produced the state of this row. Not an ordering token — see 0031.
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index("ix_merchant_projection_status", "merchant_projection", ["status"])


def downgrade() -> None:
    op.drop_index("ix_merchant_projection_status", table_name="merchant_projection")
    op.drop_table("merchant_projection")
