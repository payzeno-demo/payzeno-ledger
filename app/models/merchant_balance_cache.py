"""``merchant_balance_cache`` — the denormalised balance, maintained in-transaction.

Maintained by ``LedgerPoster.post`` **in the same transaction as the entries it writes**,
so it can never lag a committed posting. ``LedgerAuditJob`` invariant 3 recomputes it from
``ledger_entry`` nightly and compares to zero difference.

This is the table behind the ``LedgerBalanceCacheDrift`` alarm that actually paged
``apager`` at 01:26 on the night of PAY-2041 — the single alarm that fired during the
sev1, and it fired for the wrong reason: the balances were inflated because settlements
had been posted twice, not because the cache had drifted from the entries. The cache was
perfectly consistent with a ledger that was itself wrong. Before migration ``0023`` the
alarm had no backing schema at all.

Cold since ``mhandover`` handed over; ``mregression`` owns the drift query now.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Currency


class MerchantBalanceCache(Base):
    """One row per ``(merchant_id, currency, livemode)``.

    ``livemode`` is part of the primary key rather than a plain column: test-mode money
    must never be counted into a balance that a payout can be drawn against.
    """

    __tablename__ = "merchant_balance_cache"
    entity_name: ClassVar[str] = "merchant_balance_cache"

    currency: Mapped[str] = mapped_column(Currency, primary_key=True)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    def is_negative(self) -> bool:
        """Whether the merchant owes Payzeno money.

        For a B2B acquirer this is the primary source of credit loss, which is why it is
        a first-class column rather than a computed sign on ``available_minor``.
        """
        return self.negative_balance_minor > 0

    def total_held_minor(self) -> int:
        """Everything the merchant has that they cannot be paid out today."""
        return self.pending_minor + self.reserved_minor + self.disputed_minor

