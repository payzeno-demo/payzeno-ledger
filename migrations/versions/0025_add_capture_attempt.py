"""add capture attempt

PAY-2060, INC hardening — and the control that actually sits on the double-capture path.

``uq_charge_processor_reference`` in ``payzeno_api`` is not a defence against this: it lives
in a different database, and the duplicate capture is issued from **here**, by
``SettlementPoster``, against a service that only holds a ``settlement_charge``
projection. The ledger never writes ``charge``, so that index is never consulted on the
path that double-charges.

This table is. The row is inserted and committed **before** the acquirer call, under
``uq_capture_attempt_key (acquirer, acquirer_idempotency_key)``, so a second capture fails
on insert before any HTTP request leaves the process.

``DeferredCaptureJob`` (30s) drives the ``pending`` rows, which is what makes the external
call happen *outside* the business transaction: a transaction that aborts after the claim
can no longer leave a charged cardholder with no ledger row.

``indeterminate`` is a real state and not a synonym for failure. A timeout on a capture is
the one case where you do not know whether the cardholder was charged; those are resolved
by ``ProcessorClient.get_capture_status``, never by re-issuing. That is the second,
independent double-charge mechanism, and neither PR #171 nor #172 touched it.

Revision ID: 0025
Revises: 0024
Create Date: month 9 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status = sa.Enum(
        "pending", "captured", "failed", "indeterminate", name="capture_attempt_status"
    )

    op.create_table(
        "capture_attempt",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("charge_id", sa.Text(), nullable=False),
        # Nullable: a capture can be issued outside reconciliation.
        sa.Column(
            "item_id",
            sa.Text(),
            sa.ForeignKey("reconciliation_item.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "acquirer", postgresql.ENUM(name="acquirer", create_type=False), nullable=False
        ),
        # The key we send the acquirer. Both of ours honour it, which is the second half
        # of the guarantee — ours stops the call, theirs stops the charge.
        sa.Column("acquirer_idempotency_key", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", status, nullable=False, server_default="pending"),
        sa.Column("response_reference", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "uq_capture_attempt_key",
        "capture_attempt",
        ["acquirer", "acquirer_idempotency_key"],
        unique=True,
    )
    op.execute(
        """
        CREATE INDEX pix_capture_attempt_pending
            ON capture_attempt (requested_at)
         WHERE status IN ('pending', 'indeterminate')
        """
    )
    op.create_index("ix_capture_attempt_charge", "capture_attempt", ["charge_id"])
    op.create_index("ix_capture_attempt_item", "capture_attempt", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_capture_attempt_item", table_name="capture_attempt")
    op.drop_index("ix_capture_attempt_charge", table_name="capture_attempt")
    op.execute("DROP INDEX IF EXISTS pix_capture_attempt_pending")
    op.drop_index("uq_capture_attempt_key", table_name="capture_attempt")
    op.drop_table("capture_attempt")
    sa.Enum(name="capture_attempt_status").drop(op.get_bind(), checkfirst=True)
