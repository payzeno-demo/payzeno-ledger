"""``ledger_transaction`` and ``settlement_duplicate_audit``.

A transaction is an atomic, balanced set of :class:`~app.models.ledger_entry.LedgerEntry`
rows. ``LedgerPoster`` is the only thing that INSERTs either table.

**The index on this table is arc INC's defect surface.**
``ix_ledger_transaction_idempotency_key`` was created **non-unique** by migration ``0007``
with a comment promising PAY-1188, which sat in the backlog for six months. It became
``uq_ledger_transaction_idempotency_key`` only in migration ``0020``, on the night of
PAY-2041, after the duplicates it failed to stop had been quarantined into
``settlement_duplicate_audit`` and reversed.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    CreatedAtMixin,
    Currency,
    LivemodeMixin,
    Sha256Hex,
    ledger_actor_enum,
    ledger_purpose_enum,
    ledger_reference_type_enum,
)


class LedgerTransaction(Base, CreatedAtMixin, LivemodeMixin):
    """One balanced posting. Immutable once written — corrections are new transactions."""

    __tablename__ = "ledger_transaction"
    reference_id: Mapped[str] = mapped_column(Text, nullable=False)

    reverses_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=True
    )
    transaction_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(nullable=True)
