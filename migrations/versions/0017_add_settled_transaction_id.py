"""add settled transaction id to reconciliation item

Which ledger transaction settled this item. Until now the link only existed in the other
direction — ``ledger_transaction.reference_id`` pointing at the item — and answering "show
me the posting for this line" meant an unindexed reverse lookup.

It becomes invariant (4) of `data-model.md` §6: every item in ``settled`` has a non-null
``settled_transaction_id`` that exists.

Worth noting for later: this column is what makes the incident *findable*. The duplicate
transactions were discovered by grouping ``ledger_transaction`` on ``idempotency_key``, but
the 1,847 affected items were identified through this pointer, and the compensating
reversals were matched back to them the same way.

Revision ID: 0017
Revises: 0016
Create Date: month 8 — mhandover
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reconciliation_item",
        sa.Column("settled_transaction_id", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_reconciliation_item_settled_transaction_id",
        "reconciliation_item",
        "ledger_transaction",
        ["settled_transaction_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Backfill from the other direction, for items settled before the column existed.
    op.execute(
        """
        UPDATE reconciliation_item ri
           SET settled_transaction_id = lt.id
          FROM ledger_transaction lt
         WHERE lt.reference_type = 'reconciliation_item'
           AND lt.reference_id = ri.id
           AND lt.purpose = 'settle'
           AND ri.status = 'settled'
           AND ri.settled_transaction_id IS NULL
        """
    )
    # Partial: only settled items have one, and they are a minority for the first year.
    op.execute(
        """
        CREATE INDEX pix_reconciliation_item_settled_txn
            ON reconciliation_item (settled_transaction_id)
         WHERE settled_transaction_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pix_reconciliation_item_settled_txn")
    op.drop_constraint(
        "fk_reconciliation_item_settled_transaction_id",
        "reconciliation_item",
        type_="foreignkey",
    )
    op.drop_column("reconciliation_item", "settled_transaction_id")
