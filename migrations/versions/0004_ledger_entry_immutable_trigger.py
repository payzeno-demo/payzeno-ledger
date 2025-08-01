"""ledger entry immutable trigger

``ledger_entry`` is append-only. Not by convention, not "enforced in the service layer" —
by a ``BEFORE UPDATE OR DELETE`` trigger that raises ``P0001``.

The argument for putting it in the database rather than in ``LedgerPoster``: the ops CLI,
every migration in this directory, and anyone with a psql session all write rows without
going through Python. A correction is a compensating ``reversal`` transaction, and
migration ``0020`` had to reverse the incident's duplicates rather than delete them
precisely because of this trigger.

Revision ID: 0004
Revises: 0003
Create Date: month 2 — dhotfix
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_ledger_entry_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ledger_entry is append-only'
                USING ERRCODE = 'P0001',
                      HINT = 'post a compensating reversal transaction instead';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_entry_immutable
        BEFORE UPDATE OR DELETE ON ledger_entry
        FOR EACH ROW EXECUTE FUNCTION reject_ledger_entry_mutation()
        """
    )


def downgrade() -> None:
    # Reversible, and it should stay reversible: a restore-from-backup drill that cannot
    # bulk-load entries is a restore drill that fails at 3am.
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entry_immutable ON ledger_entry")
    op.execute("DROP FUNCTION IF EXISTS reject_ledger_entry_mutation()")
