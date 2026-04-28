"""add duplicate settlement audit

PAY-2050, 02:26, on the night of the sev1. Creates the quarantine table and the function
that unwinds a duplicate, so that ``0020`` has somewhere to put what it finds before it
adds the constraint that would otherwise reject the data.

Reversal and not deletion: ``ledger_entry`` is append-only (``0004``), and even if it were
not, deleting the second posting would erase the only record that the cardholder was
charged twice. ``reverse_duplicate_transactions()`` posts a mirrored transaction for every
quarantined row and links the two.

> Review note (#172): the first push had this revision and ``0020`` the other way round —
> the unique-index migration referenced ``settlement_duplicate_audit`` before it existed.
> Caught in review at 02:40; the revision ids and ``down_revision`` pointers were swapped
> in a follow-up commit on the same branch.

Revision ID: 0019
Revises: 0018
Create Date: month 9, 02:26 — nmigration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settlement_duplicate_audit",
        sa.Column("transaction_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("reference_id", sa.Text(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Null until reverse_duplicate_transactions() has posted the compensating entry.
        sa.Column("reversed_transaction_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_settlement_duplicate_audit_key",
        "settlement_duplicate_audit",
        ["idempotency_key"],
    )
    op.execute(
        """
        CREATE INDEX pix_settlement_duplicate_audit_unreversed
            ON settlement_duplicate_audit (detected_at)
         WHERE reversed_transaction_id IS NULL
        """
    )

    # Posts a mirrored transaction for every quarantined duplicate: same amounts, flipped
    # directions, purpose 'reversal', reverses_transaction_id pointing at the duplicate.
    # Written in SQL rather than in Python because it has to run inside the migration,
    # before the unique index exists and before the application is allowed back on.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reverse_duplicate_transactions() RETURNS integer AS $$
        DECLARE
            dup           RECORD;
            reversal_id   text;
            reversed_rows integer := 0;
        BEGIN
            FOR dup IN
                SELECT a.transaction_id, t.purpose, t.merchant_id, t.currency,
                       t.reference_type, t.reference_id
                  FROM settlement_duplicate_audit a
                  JOIN ledger_transaction t ON t.id = a.transaction_id
                 WHERE a.reversed_transaction_id IS NULL
            LOOP
                reversal_id := 'txn_rev_' || dup.transaction_id;

                INSERT INTO ledger_transaction (
                    id, idempotency_key, purpose, merchant_id, currency,
                    reference_type, reference_id, reverses_transaction_id,
                    created_by, posted_at, created_at
                ) VALUES (
                    reversal_id,
                    'reversal:' || dup.transaction_id || ':',
                    'reversal', dup.merchant_id, dup.currency,
                    dup.reference_type, dup.reference_id, dup.transaction_id,
                    'admin', now(), now()
                )
                ON CONFLICT DO NOTHING;

                -- Mirror every leg. Direction flips; amount and account do not.
                INSERT INTO ledger_entry (
                    id, transaction_id, account_id, direction, amount_minor,
                    currency, sequence, created_at
                )
                SELECT 'le_rev_' || e.id,
                       reversal_id,
                       e.account_id,
                       CASE WHEN e.direction = 'debit' THEN 'credit' ELSE 'debit' END::entry_direction,
                       e.amount_minor,
                       e.currency,
                       e.sequence,
                       now()
                  FROM ledger_entry e
                 WHERE e.transaction_id = dup.transaction_id
                ON CONFLICT DO NOTHING;

                UPDATE settlement_duplicate_audit
                   SET reversed_transaction_id = reversal_id
                 WHERE transaction_id = dup.transaction_id;

                reversed_rows := reversed_rows + 1;
            END LOOP;

            RETURN reversed_rows;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reverse_duplicate_transactions()")
    op.execute("DROP INDEX IF EXISTS pix_settlement_duplicate_audit_unreversed")
    op.drop_index(
        "ix_settlement_duplicate_audit_key", table_name="settlement_duplicate_audit"
    )
    op.drop_table("settlement_duplicate_audit")
