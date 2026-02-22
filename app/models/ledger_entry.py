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
    entity_name: ClassVar[str] = "ledger_entry"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    #: Direction carries the sign. `amount_minor` is always strictly positive.
    direction: Mapped[str] = mapped_column(entry_direction_enum, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    #: 0-based ordinal within the transaction. Stable, so a trial-balance report and the
    #: ops CLI print the legs of a posting in the order the rule built them.
    sequence: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    __table_args__ = (
        # Invariant 4 of domain-model.md §7, at the storage layer. LedgerPoster raises
        # NegativeAmountError before it gets here; this is the backstop for the ops CLI
        # and the Alembic data migrations, which write entries through raw SQL.
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        # Two legs of one transaction can never share a sequence, so a partially-retried
        # insert cannot produce a transaction with three legs numbered 0, 1, 1.
        Index("uq_ledger_entry_txn_sequence", "transaction_id", "sequence", unique=True),
        # arc PERF, migration 0015. LedgerEntryRepository.sum_by_account_and_purpose and
        # trial_balance_by_currency both drive off this; before it, computing one
        # merchant's balance was a sequential scan of the whole table.
        Index(
            "ix_ledger_entry_account_created",
            "account_id",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
        Index("ix_ledger_entry_transaction_id", "transaction_id"),
    )

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
