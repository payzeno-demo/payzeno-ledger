"""add the columns the models already map

Four columns the code has been reading and writing that no earlier migration created.
Each one failed on first contact with a real database:

* ``reconciliation_item.acquirer`` — the matcher scopes its charge lookups by it
  (``uq_settlement_charge_network_txn`` and ``ix_settlement_charge_processor_reference``
  are both per acquirer) and ``SettlementPoster`` sends it on every
  ``confirm_settlement``. Denormalised from the batch, like ``currency`` and ``livemode``:
  one batch is one acquirer file, so the value cannot drift.
* ``merchant_projection.livemode`` and ``settlement_charge.livemode`` — ``0032`` says
  "the projections already have it", and they did not. Both upserts write it.
* ``event_outbox.livemode`` — ``OutboxPublisher`` stamps ``EventEnvelope.livemode`` onto
  the row it writes, like every other ``LivemodeMixin`` table.

The three ``livemode`` columns arrive ``NOT NULL DEFAULT true``, the same shape as ``0022``
onwards: everything that exists today is live, and a constant default is a catalogue-only
change on Postgres 11+, so the running fleet keeps inserting without naming the column.

``reconciliation_item.acquirer`` has no constant to default to, so it is two-phase as
``0032`` was: added nullable and backfilled from the batch here; ``NOT NULL`` in the next
release, once every task writes it.

Revision ID: 0034
Revises: 0033
Create Date: month 12 — runtime bring-up
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

#: Tables that gain ``livemode boolean not null default true`` here.
LIVEMODE_TABLES = ("merchant_projection", "settlement_charge", "event_outbox")


def upgrade() -> None:
    for table in LIVEMODE_TABLES:
        op.add_column(
            table,
            sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        )

    op.add_column(
        "reconciliation_item",
        sa.Column(
            "acquirer", postgresql.ENUM(name="acquirer", create_type=False), nullable=True
        ),
    )
    op.execute(
        """
        UPDATE reconciliation_item i
           SET acquirer = b.acquirer
          FROM settlement_batch b
         WHERE b.id = i.batch_id
           AND i.acquirer IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("reconciliation_item", "acquirer")
    for table in reversed(LIVEMODE_TABLES):
        op.drop_column(table, "livemode")
