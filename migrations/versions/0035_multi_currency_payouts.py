"""multi-currency payouts: FX accounts, payout source columns, in-flight index on the balance

RFC platform-v2 §7. A payout's ``currency`` is what lands in the bank; when the bank account
is in another currency than the balance being paid out, the ledger converts at payout time.

* ``account_type`` gains ``fx_clearing`` (debit-normal) and ``fx_revenue`` (credit-normal),
  both platform accounts, one per currency. A converted payout posts two transactions, one
  per currency, each balanced on its own through ``fx_clearing`` — invariant 3 (one
  currency per transaction) is untouched.
* ``payout`` gains ``source_amount_minor``, ``source_currency``, ``fx_rate`` (the three the
  ``Payout`` contract carries) and ``fx_spread_minor`` (internal: what ``fx_revenue`` was
  credited, so a failure or return reverses exactly that). All null on an unconverted
  payout, all set on a converted one — ``chk_payout_fx_complete``.
* ``pix_payout_in_flight`` is re-keyed from ``currency`` to
  ``coalesce(source_currency, currency)``: the balance the payout drew from, which is the
  currency ``create_payout``'s advisory lock is taken on. Keyed on the bank currency, a EUR
  balance and a USD balance swept into one USD account in the same pass would collide. For
  every existing row the expression equals ``currency``, so the new index builds over the
  same data the old one already proved unique. Built under a new name and swapped, so
  there is no window without it.

``ALTER TYPE ... ADD VALUE`` needs its own committed statement (0026), and the index swap is
``CONCURRENTLY`` (0033), so this migration commits twice.

Revision ID: 0035
Revises: 0034
Create Date: RFC platform-v2 — multi-currency
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

FX_ACCOUNT_TYPES = ("fx_clearing", "fx_revenue")


def upgrade() -> None:
    op.execute("COMMIT")
    for account_type in FX_ACCOUNT_TYPES:
        op.execute(f"ALTER TYPE account_type ADD VALUE IF NOT EXISTS '{account_type}'")

    op.add_column("payout", sa.Column("source_amount_minor", sa.BigInteger(), nullable=True))
    op.add_column("payout", sa.Column("source_currency", sa.CHAR(3), nullable=True))
    op.add_column("payout", sa.Column("fx_rate", sa.Numeric(20, 10), nullable=True))
    op.add_column("payout", sa.Column("fx_spread_minor", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("chk_payout_fx_complete"),
        "payout",
        "(source_currency is null and source_amount_minor is null and fx_rate is null"
        " and fx_spread_minor is null) or (source_currency is not null"
        " and source_amount_minor > 0 and fx_rate > 0 and fx_spread_minor >= 0)",
    )

    op.execute("COMMIT")
    op.execute(
        """
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS pix_payout_in_flight_0035
            ON payout (merchant_id, (coalesce(source_currency, currency)), livemode)
         WHERE status IN ('scheduled', 'in_transit')
        """
    )
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_payout_in_flight")
    op.execute("ALTER INDEX pix_payout_in_flight_0035 RENAME TO pix_payout_in_flight")


def downgrade() -> None:
    # The old key is narrower than the new one only for converted payouts. Cancel any
    # converted payout still `scheduled` that would collide with another in-flight payout
    # on its bank currency, as 0033 did, so the old unique index can be rebuilt.
    op.execute(
        """
        UPDATE payout p
           SET status = 'canceled',
               failure_message = 'superseded — downgrade of migration 0035',
               updated_at = now()
         WHERE p.status = 'scheduled'
           AND p.source_currency IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM payout o
                WHERE o.merchant_id = p.merchant_id
                  AND o.currency = p.currency
                  AND o.livemode = p.livemode
                  AND o.id <> p.id
                  AND o.status IN ('scheduled', 'in_transit')
           )
        """
    )
    op.execute("COMMIT")
    op.execute(
        """
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS pix_payout_in_flight_0033
            ON payout (merchant_id, currency, livemode)
         WHERE status IN ('scheduled', 'in_transit')
        """
    )
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS pix_payout_in_flight")
    op.execute("ALTER INDEX pix_payout_in_flight_0033 RENAME TO pix_payout_in_flight")

    op.drop_constraint(op.f("chk_payout_fx_complete"), "payout", type_="check")
    op.drop_column("payout", "fx_spread_minor")
    op.drop_column("payout", "fx_rate")
    op.drop_column("payout", "source_currency")
    op.drop_column("payout", "source_amount_minor")
    # Postgres cannot drop an enum value. `fx_clearing` and `fx_revenue` stay on
    # `account_type`, unused, exactly as 0026's statuses would on a downgrade.
