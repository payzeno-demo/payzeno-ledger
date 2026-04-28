"""unique transaction idempotency key

PAY-2050. The structural fix. ``ix_ledger_transaction_idempotency_key`` has been
**non-unique since ``0007``** — see the note in that file, and the PAY-1188 follow-up that
sat in the backlog for six months and was closed as a duplicate of this ticket tonight.

Three steps, in this order, because the constraint cannot exist while the duplicates do:
quarantine, reverse, constrain.

Also adds ``request_fingerprint``: ``claim_idempotency_key`` compares it, and without a
stored value ``POST /internal/v1/transactions``'s documented behaviour — 200 on an
identical body, 409 on a repeat with a different one — has nothing to compare against.
Note this is orthogonal to the defect: even with the unique index, the constraint detects
**key reuse**, not body divergence.

Revision ID: 0020
Revises: 0019
Create Date: month 9, 02:26 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
def upgrade() -> None:
    op.add_column(
        "ledger_transaction",
        sa.Column(
            "request_fingerprint",
            sa.CHAR(64),
            nullable=False,
            server_default="0" * 64,
        ),
    )

    # 1. quarantine the duplicates the incident produced
    op.execute(
        """
        INSERT INTO settlement_duplicate_audit (transaction_id, idempotency_key, detected_at)
        SELECT t.id, t.idempotency_key, now()
        FROM ledger_transaction t
        JOIN (
            SELECT idempotency_key, min(posted_at) AS first_posted
            FROM ledger_transaction GROUP BY idempotency_key HAVING count(*) > 1
        ) d ON d.idempotency_key = t.idempotency_key
        WHERE t.posted_at > d.first_posted
        """
    )
    # 2. reverse them (compensating entries, not deletes — ledger_entry is append-only)
    op.execute("SELECT reverse_duplicate_transactions()")
    # 2a. the reversals share the loser's key, so they would collide with it. Re-key them
    #     onto their own deterministic reversal key before the constraint lands.
    op.execute(
        """
        UPDATE ledger_transaction
           SET idempotency_key = 'reversal:' || reverses_transaction_id || ':'
         WHERE reverses_transaction_id IS NOT NULL
           AND purpose = 'reversal'
        """
    )
    # 2b. the losing originals keep their history but must stop colliding. Suffix them
    #     with their own id — unique by construction, and greppable.
    op.execute(
        """
        UPDATE ledger_transaction t
           SET idempotency_key = t.idempotency_key || '#dup:' || t.id
          FROM settlement_duplicate_audit a
         WHERE a.transaction_id = t.id
        """
    )
    # 3. now the constraint can exist
    op.drop_index("ix_ledger_transaction_idempotency_key", table_name="ledger_transaction")
    op.create_index(
        "uq_ledger_transaction_idempotency_key",
        "ledger_transaction",
        ["idempotency_key"],
        unique=True,
    )


