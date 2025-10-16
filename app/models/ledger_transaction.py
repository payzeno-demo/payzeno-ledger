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
    purpose: Mapped[str] = mapped_column(ledger_purpose_enum, nullable=False)
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

    entity_name: ClassVar[str] = "settlement_duplicate_audit"

    transaction_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(nullable=True)
    __table_args__ = (
        Index("ix_settlement_duplicate_audit_key", "idempotency_key"),
        Index(
            "pix_settlement_duplicate_audit_unreversed",
            "detected_at",
            postgresql_where="reversed_transaction_id is null",
        ),
    )

