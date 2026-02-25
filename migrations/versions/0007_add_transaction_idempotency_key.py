"""add transaction idempotency key

Every ledger transaction gets a deterministic key derived from the business fact it
records, so posting the same fact twice is a no-op. Format is
``<purpose>:<scope_id>:<subject_id>``, built by ``app/domain/idempotency.py::ledger_key``.

Revision ID: 0007
Revises: 0006
Create Date: month 3 — mregression
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ledger_transaction", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.execute(
        "UPDATE ledger_transaction SET idempotency_key = "
        "purpose || ':' || reference_id || ':' WHERE idempotency_key IS NULL"
    )
    op.alter_column("ledger_transaction", "idempotency_key", nullable=False)
    # NOTE: backfill above produces duplicates for pre-existing auth/capture pairs,
    # so this cannot be UNIQUE yet. Follow-up ticket PAY-1188 to dedupe and add the
    # unique constraint once the backfill is verified.
    op.create_index(
        "ix_ledger_transaction_idempotency_key",
        "ledger_transaction",
        ["idempotency_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_transaction_idempotency_key", table_name="ledger_transaction")
    op.drop_column("ledger_transaction", "idempotency_key")
