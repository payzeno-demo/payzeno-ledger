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
    entity_name: ClassVar[str] = "ledger_transaction"

    id: Mapped[str] = mapped_column(Text, primary_key=True)

    #: Deterministic, derived from the business fact — never from an attempt counter, a
    #: clock or a random value. `app/domain/idempotency.py::ledger_key` builds it.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    purpose: Mapped[str] = mapped_column(ledger_purpose_enum, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)

    #: Polymorphic pointer at the API-side object this posting is about.
    reference_type: Mapped[str] = mapped_column(ledger_reference_type_enum, nullable=False)
    reference_id: Mapped[str] = mapped_column(Text, nullable=False)

    reverses_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=True
    )
    created_by: Mapped[str] = mapped_column(
        ledger_actor_enum, nullable=False, server_default="system"
    )
    posted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # 0007 created this NON-UNIQUE, because the backfill
        #   purpose || ':' || reference_id || ':'
        # produced duplicates for pre-existing auth/capture pairs. 0020 dropped it and
        # created uq_ledger_transaction_idempotency_key CONCURRENTLY in its place, at
        # 03:05 on the night of the incident, against 41M rows, in 14 seconds.
        Index(
            "uq_ledger_transaction_idempotency_key",
            "idempotency_key",
            unique=True,
        ),
        Index("ix_ledger_transaction_reference", "reference_type", "reference_id"),
        Index(
            "ix_ledger_transaction_merchant_posted",
            "merchant_id",
            "posted_at",
            postgresql_ops={"posted_at": "DESC"},
        ),
        Index(
            "ix_ledger_transaction_purpose_posted",
            "purpose",
            "posted_at",
            postgresql_ops={"posted_at": "DESC"},
        ),
    )

    def is_reversal(self) -> bool:
        """True when this transaction compensates another one."""
        return self.reverses_transaction_id is not None

    def scope_key(self) -> str:
        """The middle segment of the idempotency key — batch id, charge id, payout id.

        The runbook's duplicate-finding query groups on this to answer "which batch did
        the duplicates come from" without joining ``reconciliation_item``.
        """
        parts = self.idempotency_key.split(":")
        return parts[1] if len(parts) >= 2 else ""


class SettlementDuplicateAudit(Base):
    """Quarantine table for duplicate settlements — migration ``0019``, PR #172.

    Migration ``0020`` cannot create a unique index while duplicates exist, so it first
    copies every transaction whose ``posted_at`` is later than the earliest one sharing
    its ``idempotency_key`` into this table, then calls
    ``reverse_duplicate_transactions()`` to post compensating entries for them — never a
    ``DELETE``, because ``ledger_entry`` is append-only.

    The 1,847 rows from the night of PAY-2041 are still in here. They are what the
    postmortem's blast-radius numbers are counted from.
    """

    __tablename__ = "settlement_duplicate_audit"
    entity_name: ClassVar[str] = "settlement_duplicate_audit"

    transaction_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    reference_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_minor: Mapped[int | None] = mapped_column(nullable=True)
    detected_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Null until `reverse_duplicate_transactions()` has posted the compensating entry.
    reversed_transaction_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_settlement_duplicate_audit_key", "idempotency_key"),
        Index(
            "pix_settlement_duplicate_audit_unreversed",
            "detected_at",
            postgresql_where="reversed_transaction_id is null",
        ),
    )

    def is_reversed(self) -> bool:
        """Whether the compensating transaction has been posted."""
        return self.reversed_transaction_id is not None
