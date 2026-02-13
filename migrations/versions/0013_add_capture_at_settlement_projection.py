"""add capture_at_settlement to both projections

Deferred capture: payzeno-api authorises the card and leaves it uncaptured, and **this
service** issues the capture when the acquirer's settlement line arrives. Eleven travel and
lodging merchants, all in the MCC ranges the schemes grant thirty-day authorisation
validity for.

It goes on both projections and the distinction matters. ``merchant_projection`` carries
the current merchant setting; ``settlement_charge`` carries the value **as of
authorisation**, and ``SettlementPoster`` reads it off the charge. Flipping the merchant
flag mid-flight would otherwise decide whether an already-authorised charge gets a second
cardholder capture — which is a much worse property than the same argument applied to a
fee rate.

From here on, this service holds an irreversible cardholder side effect on the settlement
path. That is not a bug; it is the feature. It is also what turns the reconciliation race
three months from now from an accounting problem into a customer-facing one.

Revision ID: 0013
Revises: 0012
Create Date: month 7 — dhotfix
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "merchant_projection",
        sa.Column(
            "capture_at_settlement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "settlement_charge",
        sa.Column(
            "capture_at_settlement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Backfill the in-flight charges from the merchant setting. Correct exactly once,
    # here, at the moment the two definitions are still the same thing.
    op.execute(
        """
        UPDATE settlement_charge sc
           SET capture_at_settlement = mp.capture_at_settlement
          FROM merchant_projection mp
         WHERE mp.merchant_id = sc.merchant_id
           AND sc.captured_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("settlement_charge", "capture_at_settlement")
    op.drop_column("merchant_projection", "capture_at_settlement")
