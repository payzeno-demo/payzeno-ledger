"""add item backoff, line type and match method

PAY-2059, INC hardening. Two independent problems, one migration, because both are columns
on ``reconciliation_item`` and taking two ``ACCESS EXCLUSIVE`` locks on it in one week was
not worth the tidiness.

**Backoff.** ``next_attempt_at`` carries exponential backoff with jitter, and
``pix_reconciliation_item_retryable`` is re-keyed onto it. The old index ordered by
``last_attempt_at`` and did **not filter on it**, which is how four drains pulling 200
items every sixty seconds hammered an acquirer that was already returning 504s at up to
800 capture attempts a minute — during exactly the degradation that caused PAY-2041. The
drain now filters ``next_attempt_at <= now()``.

**Line types.** An acquirer settlement file is not a list of sales; it carries refunds,
chargebacks, chargeback reversals, scheme fees, adjustments and reserve movements in the
same file. Without ``line_type``, every one of those matched no charge, landed in
``orphaned``, and became manual investigation for a large fraction of every batch — and
``SettlementPoster`` built a sale posting for all of them regardless.

``match_method`` and ``matched_at`` record *how* a line was linked to a charge, which is
what makes a ``heuristic`` match visibly weaker than an exact one. ``network_reference`` is
the acquirer's copy of the network transaction id and is match strategy 2's input.

Revision ID: 0024
Revises: 0023
Create Date: month 9 — mregression
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

LINE_TYPES = (
    "sale",
    "refund",
    "chargeback",
    "chargeback_reversal",
    "scheme_fee",
    "adjustment",
    "reserve_hold",
    "reserve_release",
)

MATCH_METHODS = ("exact_reference", "network_txn", "heuristic", "manual", "unmatched")


def upgrade() -> None:
    line_type = sa.Enum(*LINE_TYPES, name="reconciliation_line_type")
    match_method = sa.Enum(*MATCH_METHODS, name="reconciliation_match_method")
    line_type.create(op.get_bind(), checkfirst=True)
    match_method.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "reconciliation_item",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "reconciliation_item",
        sa.Column("line_type", line_type, nullable=False, server_default="sale"),
    )
    op.add_column(
        "reconciliation_item",
        sa.Column(
            "match_method", match_method, nullable=False, server_default="unmatched"
        ),
    )
    op.add_column(
        "reconciliation_item",
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reconciliation_item", sa.Column("network_reference", sa.Text(), nullable=True)
    )

    # Everything that already settled matched somehow, and the only strategy that existed
    # was the exact reference one. Recording it as `unmatched` would be a lie the backlog
    # screen then shows to an operator.
    op.execute(
        """
        UPDATE reconciliation_item
           SET match_method = 'exact_reference',
               matched_at = COALESCE(last_attempt_at, updated_at)
         WHERE charge_id IS NOT NULL
        """
    )

    op.create_index(
        "ix_reconciliation_item_line_type",
        "reconciliation_item",
        ["batch_id", "line_type"],
    )

    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_reconciliation_item_retryable")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_reconciliation_item_retryable
            ON reconciliation_item (batch_id, next_attempt_at)
         WHERE status IN ('pending', 'retryable')
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_reconciliation_item_retryable")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_reconciliation_item_retryable
            ON reconciliation_item (batch_id, last_attempt_at)
         WHERE status IN ('pending', 'retryable')
        """
    )
    op.drop_index("ix_reconciliation_item_line_type", table_name="reconciliation_item")
    op.drop_column("reconciliation_item", "network_reference")
    op.drop_column("reconciliation_item", "matched_at")
    op.drop_column("reconciliation_item", "match_method")
    op.drop_column("reconciliation_item", "line_type")
    op.drop_column("reconciliation_item", "next_attempt_at")
    sa.Enum(name="reconciliation_match_method").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="reconciliation_line_type").drop(op.get_bind(), checkfirst=True)
