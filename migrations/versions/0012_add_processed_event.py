"""add processed event and event outbox

Both halves of the bus boundary in one migration, because they arrived with the first
consumer and neither is useful alone.

``processed_event`` PK is ``(event_id, consumer)``. Two consumers in this service handle
overlapping event sets; with ``event_id`` alone the second one could never mark anything
processed and the first would starve it.

``event_outbox`` carries ``next_attempt_at``, ``claimed_at`` / ``claimed_by`` and
``dead_at`` from the start. The drain runs in all four ECS tasks, so the claim is
``FOR UPDATE SKIP LOCKED`` and an unclaimed ``WHERE published_at IS NULL`` select would
publish every event four times. Without ``next_attempt_at``, one permanently failing event
is re-selected every five seconds forever and takes the drain's whole budget with it.

Revision ID: 0012
Revises: 0011
Create Date: month 6 — mregression
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_event",
        sa.Column("event_id", sa.Text(), primary_key=True),
        # In the key on purpose. See the module docstring.
        sa.Column("consumer", sa.Text(), primary_key=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_processed_event_processed_at", "processed_event", ["processed_at"]
    )

    op.create_table(
        "event_outbox",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("version", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("merchant_id", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("causation_id", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.execute(
        """
        CREATE INDEX pix_event_outbox_due
            ON event_outbox (next_attempt_at)
         WHERE published_at IS NULL AND dead_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_event_outbox_type_occurred
            ON event_outbox (type, occurred_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX pix_event_outbox_dead
            ON event_outbox (dead_at)
         WHERE dead_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pix_event_outbox_dead")
    op.execute("DROP INDEX IF EXISTS ix_event_outbox_type_occurred")
    op.execute("DROP INDEX IF EXISTS pix_event_outbox_due")
    op.drop_table("event_outbox")
    op.drop_index("ix_processed_event_processed_at", table_name="processed_event")
    op.drop_table("processed_event")
