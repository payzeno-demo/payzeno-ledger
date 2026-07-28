"""make the payout in-flight index unique

The promise ``pix_payout_in_flight`` has been half-keeping since ``0018``: at most one
payout per ``(merchant, currency, livemode)`` may be in flight at a time.

``PayoutCalculator.compute_available`` subtracts in-flight payouts by reading rows a
concurrent uncommitted transaction has not written yet — the identical check-then-act shape
as PAY-2041, on the path that moves money to a bank account. ``PayoutService.create_payout``
takes ``AdvisoryLockManager.acquire_merchant_currency_lock`` before computing the balance
(advisory-then-row, per `docs/adr/0011-lock-ordering-in-the-money-path.md`), and from this
migration the unique index is the database's last word if that is ever bypassed.

Filed as a follow-up on the postmortem: "find the other places the same shape appears".
This was the one.

Deduped first — two live rows, both from a console double-click in month 6, both long since
paid. The dedupe below is written to be safe to re-run and to touch nothing terminal.

Revision ID: 0033
Revises: 0032
Create Date: month 12 — mhandover
"""

from __future__ import annotations

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cancel the younger of any in-flight pair. `scheduled` only: an `in_transit` payout
    # has left and cancelling it in the database would not bring it back.
    op.execute(
        """
        UPDATE payout p
           SET status = 'canceled',
               failure_message = 'superseded — deduped by migration 0033',
               updated_at = now()
         WHERE p.status = 'scheduled'
           AND EXISTS (
               SELECT 1 FROM payout o
                WHERE o.merchant_id = p.merchant_id
                  AND o.currency = p.currency
                  AND o.livemode = p.livemode
                  AND o.status IN ('scheduled', 'in_transit')
                  AND o.created_at < p.created_at
           )
        """
    )

    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_payout_in_flight")
    op.execute(
        """
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS pix_payout_in_flight
            ON payout (merchant_id, currency, livemode)
         WHERE status IN ('scheduled', 'in_transit')
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_payout_in_flight")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS pix_payout_in_flight
            ON payout (merchant_id, currency, livemode)
         WHERE status IN ('scheduled', 'in_transit')
        """
    )
