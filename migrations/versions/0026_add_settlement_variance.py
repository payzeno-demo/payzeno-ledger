"""add settlement variance and fee breakdown columns

Two new item statuses and four new columns, all so that ``SettlementPoster`` stops posting
the acquirer's numbers blind.

**Variance.** ``expected_gross_minor`` comes from the matched ``settlement_charge``;
``variance_minor`` is the difference. Beyond ``merchant.settlement_tolerance_minor`` the
item goes to ``variance_exceeded``, ``settlement.variance_detected`` is emitted, and
nothing is posted. Acquirers routinely settle a different amount than was authorised —
partial capture, interchange downgrade, DCC, plain file error — and a wrong file silently
accepted credits the merchant the wrong amount while the ledger stays perfectly balanced.
That is the failure mode the PAY-2041 postmortem calls undetectable, and this is the
answer to it.

**Fee breakdown.** ``interchange_minor`` and ``scheme_fee_minor`` are parsed from the
acquirer file and are what give ``apportion_fee`` its inputs. Without them the schema only
expresses blended pricing and interchange-plus apportionment has nothing to apportion.
``fee_minor`` remains the total, so ``acquirer_markup = fee_minor - interchange_minor -
scheme_fee_minor`` is derivable rather than stored.

``needs_review`` is added alongside, for heuristic matches — a hint, not an answer, and it
never auto-settles.

Revision ID: 0026
Revises: 0025
Create Date: month 10 — mregression
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on PG < 12 and cannot
    # be used in the same transaction as a query on the type on any version. Commit first
    # and add them one at a time.
    op.execute("COMMIT")
    op.execute(
        "ALTER TYPE reconciliation_item_status ADD VALUE IF NOT EXISTS 'variance_exceeded'"
    )
    op.execute(
        "ALTER TYPE reconciliation_item_status ADD VALUE IF NOT EXISTS 'needs_review'"
    )

    op.add_column(
        "reconciliation_item",
        sa.Column("expected_gross_minor", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "reconciliation_item",
        sa.Column("variance_minor", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reconciliation_item",
        sa.Column(
            "interchange_minor", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "reconciliation_item",
        sa.Column("scheme_fee_minor", sa.BigInteger(), nullable=False, server_default="0"),
    )

    # Backfill the expected amount for already-matched items so the first run after this
    # deploy does not report every historical line as a variance of its whole value.
    op.execute(
        """
        UPDATE reconciliation_item ri
           SET expected_gross_minor = sc.amount_minor,
               variance_minor = ri.gross_minor - sc.amount_minor
          FROM settlement_charge sc
         WHERE sc.charge_id = ri.charge_id
           AND ri.charge_id IS NOT NULL
           AND ri.expected_gross_minor IS NULL
        """
    )


def downgrade() -> None:
    # Enum values cannot be removed in PostgreSQL. The columns can go; the two statuses
    # stay, unused, and any row still carrying one has to be moved off it first.
    op.execute(
        """
        UPDATE reconciliation_item
           SET status = 'failed'
         WHERE status IN ('variance_exceeded', 'needs_review')
        """
    )
    op.drop_column("reconciliation_item", "scheme_fee_minor")
    op.drop_column("reconciliation_item", "interchange_minor")
    op.drop_column("reconciliation_item", "variance_minor")
    op.drop_column("reconciliation_item", "expected_gross_minor")
