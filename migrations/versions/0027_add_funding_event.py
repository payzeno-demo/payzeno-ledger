"""add funding event and batch funding columns

Cash follows the bank, not the file.

Until now ``settle`` posted on the strength of an acquirer's settlement file and nothing
ever debited ``cash`` — the ledger effectively claimed money on the acquirer's say-so. This
migration adds the bank side: ``funding_event`` rows are ingested from the statement feed,
``FundingMatchJob`` matches one to a reconciled batch within
``FUNDING_MATCH_TOLERANCE_BPS``, and only then does ``settlement_funding`` post
Dr ``cash`` / Cr ``processor_clearing``.

An acquirer that files a batch and then short-pays now leaves ``processor_clearing``
outstanding — which is exactly what a receivable account is for — instead of leaving
Payzeno paying merchants out of money that never arrived.
``PayoutCalculator.compute_available`` counts only ``funded`` batches, which is how the
guarantee reaches the merchant.

``uq_funding_event_bank_reference`` makes re-ingesting a statement idempotent. Bank feeds
re-send whole days routinely.

The ``settlement_funding`` **purpose** itself lands in ``0029`` with the other three new
purposes; ``FundingMatchJob`` stays behind ``FUNDING_MATCH_ENABLED`` until it does. Two
``ALTER TYPE ... ADD VALUE`` statements in one release was the thing review pushed back on.

Revision ID: 0027
Revises: 0026
Create Date: month 10 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute("ALTER TYPE settlement_batch_status ADD VALUE IF NOT EXISTS 'funded'")

    status = sa.Enum(
        "unmatched", "matched", "short_paid", "disputed", name="funding_event_status"
    )

    op.create_table(
        "funding_event",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "acquirer", postgresql.ENUM(name="acquirer", create_type=False), nullable=False
        ),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        # The bank's value date. A date, not an instant — banks do not settle at 14:07.
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column("bank_reference", sa.Text(), nullable=False),
        sa.Column("status", status, nullable=False, server_default="unmatched"),
        sa.Column(
            "matched_batch_id",
            sa.Text(),
            sa.ForeignKey("settlement_batch.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # Recorded even inside tolerance: an acquirer that is consistently eleven bps
        # light is a contract conversation, and it only shows up if the small ones are
        # kept.
        sa.Column("variance_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_index(
        "uq_funding_event_bank_reference",
        "funding_event",
        ["bank_reference"],
        unique=True,
    )
    op.execute(
        """
        CREATE INDEX pix_funding_event_unmatched
            ON funding_event (value_date)
         WHERE status = 'unmatched'
        """
    )
    op.create_index(
        "ix_funding_event_acquirer_value_date",
        "funding_event",
        ["acquirer", "value_date"],
    )

    op.add_column(
        "settlement_batch",
        sa.Column(
            "funded_amount_minor", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "settlement_batch",
        sa.Column("funded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "settlement_batch", sa.Column("funding_event_id", sa.Text(), nullable=True)
    )
    op.create_foreign_key(
        "fk_settlement_batch_funding_event_id",
        "settlement_batch",
        "funding_event",
        ["funding_event_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE INDEX pix_settlement_batch_unfunded
            ON settlement_batch (processing_date)
         WHERE status = 'reconciled' AND funded_at IS NULL
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_funding_event_updated_at
        BEFORE UPDATE ON funding_event
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_funding_event_updated_at ON funding_event")
    op.execute("DROP INDEX IF EXISTS pix_settlement_batch_unfunded")
    op.drop_constraint(
        "fk_settlement_batch_funding_event_id", "settlement_batch", type_="foreignkey"
    )
    op.drop_column("settlement_batch", "funding_event_id")
    op.drop_column("settlement_batch", "funded_at")
    op.drop_column("settlement_batch", "funded_amount_minor")
    op.drop_index("ix_funding_event_acquirer_value_date", table_name="funding_event")
    op.execute("DROP INDEX IF EXISTS pix_funding_event_unmatched")
    op.drop_index("uq_funding_event_bank_reference", table_name="funding_event")
    op.drop_table("funding_event")
    sa.Enum(name="funding_event_status").drop(op.get_bind(), checkfirst=True)
