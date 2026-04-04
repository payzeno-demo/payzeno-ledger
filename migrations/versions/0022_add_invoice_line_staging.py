"""add invoice line staging

arc MIG step 4. The Java biller starts pushing invoice **lines** here through
``POST /internal/v1/invoices/lines/stage``, so the ledger becomes the source of line-level
truth before it becomes the source of invoice-level truth.

Unused in production. The table is written by the staging endpoint and read by nothing:
the promotion path — staged lines becoming ledger postings — is unwritten, and whether the
ledger should also own invoice *numbering* is still an open argument on the ADR. That is
why ``invoice_number`` here is a copy of the legacy value and nothing generates one.

``uq_invoice_line_staging_source_line (merchant_id, source_invoice_public_id, line_no)`` is
the natural key. The Java side re-pushes a whole invoice whenever it is edited, so a push
must replace rather than accumulate.

``quantity`` is ``numeric(12,4)`` and is the only non-integer numeric column in this
database. It is a quantity, not money — the money columns beside it are ``bigint`` minor
units, converted from the legacy ``decimal(19,4)`` at the boundary.

Revision ID: 0022
Revises: 0021
Create Date: month 12 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
def upgrade() -> None:
    op.create_table(
        "invoice_line_staging",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("merchant_id", sa.Text(), nullable=False),
        # The legacy invoice's public_id (`inv_...`). No FK — different database, and a
        # different one again from payzeno_api.
        sa.Column("source_invoice_public_id", sa.Text(), nullable=False),
        sa.Column("invoice_number", sa.Text(), nullable=True),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(12, 4), nullable=False, server_default="1.0000"),
        sa.Column("unit_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("tax_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("source_fee_schedule_id", sa.Text(), nullable=True),
        # The raw pushed line, kept verbatim. The parity job compares our minor-unit
        # arithmetic against the legacy decimal and it needs the original to do it.
        sa.Column(
            "source_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("promoted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_index(
        "uq_invoice_line_staging_source_line",
        "invoice_line_staging",
        ["merchant_id", "source_invoice_public_id", "line_no"],
        unique=True,
    )
    op.execute(
        """
        CREATE INDEX ix_invoice_line_staging_merchant_period
            ON invoice_line_staging (merchant_id, period_start DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX pix_invoice_line_staging_unpromoted
            ON invoice_line_staging (created_at)
         WHERE promoted = false
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_invoice_line_staging_updated_at
        BEFORE UPDATE ON invoice_line_staging
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


