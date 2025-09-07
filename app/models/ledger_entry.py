"""``ledger_entry`` — the append-only leg table.

**No UPDATE, no DELETE. Ever.** Enforced by ``trg_ledger_entry_immutable``, a
``BEFORE UPDATE OR DELETE`` trigger created in migration ``0004`` that raises
``P0001 'ledger_entry is append-only'``. Corrections are compensating ``reversal``
transactions, which is why migration ``0020`` reverses the incident's duplicates instead
of deleting them.

``ix_ledger_entry_account_created`` is arc PERF (migration ``0015``): the balance query
was scanning the whole table per merchant.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, Currency, LivemodeMixin, entry_direction_enum


class LedgerEntry(Base, CreatedAtMixin, LivemodeMixin):
    """One debit or credit leg of a :class:`~app.models.ledger_transaction.LedgerTransaction`."""

    __tablename__ = "ledger_entry"
    transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    def signed_minor(self) -> int:
        """Debit-positive signed amount, for balance arithmetic.

        The nightly trial balance sums this per currency and per livemode and asserts it
        is exactly zero (invariant 2 of `data-model.md` §6). Note that a *duplicate*
        settlement is internally balanced and therefore passes — which is the single
        most-quoted line in the PAY-2041 postmortem.
        """
        return self.amount_minor if self.direction == "debit" else -self.amount_minor

    def is_debit(self) -> bool:
        """True for the debit side."""
        return self.direction == "debit"
