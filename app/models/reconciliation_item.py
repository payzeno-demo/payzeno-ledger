"""``reconciliation_item`` — one acquirer line, the atom reconciliation settles.

::

    pending ─▶ settling ─▶ settled            (terminal)
                  │
                  ├─▶ retryable ─▶ settling   (bounded: attempt_count < 6, backoff)
                  ├─▶ failed                  (terminal)
                  ├─▶ variance_exceeded       (outside merchant tolerance — manual)
                  ├─▶ needs_review            (heuristic match only — manual)
                  └─▶ orphaned                (no matching charge — manual)

``pix_reconciliation_item_retryable`` is the index the retry drain scans. Migration
``0014`` (arc PERF) created it and took the drain's p99 from 12s to 40ms — which is
exactly what made the drain fast enough to run *concurrently* with a sweep instead of
trailing it, and therefore what made PAY-2041 possible. Migration ``0024`` re-keyed it
onto ``next_attempt_at`` so the drain filters on backoff rather than ordering by
``last_attempt_at`` and hammering a degraded acquirer.

> There is **no unique index on `charge_id`** here and there must not be — a charge
> legitimately appears in two batches (original + a chargeback representment). This is
> exactly why the duplicate cannot be caught at this table, and it is why a reviewer
> looking at ``reconciliation_item`` alone concludes the design is safe.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    TimestampMixin,
    reconciliation_item_status_enum,
    reconciliation_line_type_enum,
    reconciliation_match_method_enum,
)


class ReconciliationItem(Base, TimestampMixin, LivemodeMixin):
    """One line of an acquirer settlement file."""

    __tablename__ = "reconciliation_item"
    entity_name: ClassVar[str] = "reconciliation_item"

    batch_id: Mapped[str] = mapped_column(
        Text, ForeignKey("settlement_batch.id", ondelete="RESTRICT"), nullable=False
    )
    #: Null when orphaned, and null by construction on every non-sale line — scheme fee
    #: and adjustment lines have no charge. SettlementPoster's orphan guard runs BEFORE
    #: the projection read for exactly this reason.
    charge_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    merchant_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Added by 0024. Dispatches through POSTING_RULE_BY_LINE_TYPE; without it every
    #: refund, chargeback and fee line matches no charge and lands in `orphaned`.
    line_type: Mapped[str] = mapped_column(
        reconciliation_line_type_enum, nullable=False, server_default="sale"
    )

    gross_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: What the acquirer kept = interchange + scheme + acquirer markup. An expense.
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    scheme_fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    net_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: From the matched `settlement_charge`; null while unmatched. Migration 0026.
    expected_gross_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    acquirer_reference: Mapped[str] = mapped_column(Text, nullable=False)
    #: The acquirer's copy of `network_transaction_id`, for match strategy 2.
    network_reference: Mapped[str | None] = mapped_column(Text, nullable=True)

    match_method: Mapped[str] = mapped_column(
        reconciliation_match_method_enum, nullable=False, server_default="unmatched"
    )
    matched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Exponential backoff with jitter, written by `app/domain/backoff.py`. The drain
    #: filters `next_attempt_at <= now()`; the pre-0024 index ordered by
    #: `last_attempt_at` without filtering on it, which is what let four drains hammer a
    #: degraded acquirer at up to 800 capture attempts a minute.
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    settled_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_reconciliation_item_acquirer_ref",
            "batch_id",
            "acquirer_reference",
            unique=True,
        ),
        Index("ix_reconciliation_item_batch_status", "batch_id", "status"),
        # arc PERF (0014), re-keyed onto next_attempt_at by 0024 (PAY-2059).
        Index(
            "pix_reconciliation_item_retryable",
            "batch_id",
            "next_attempt_at",
            postgresql_where="status in ('pending','retryable')",
        ),
        # DELIBERATELY NON-UNIQUE. A charge legitimately appears in two batches:
        # the original sale and a later chargeback representment.
        Index("ix_reconciliation_item_charge_id", "charge_id"),
        Index("ix_reconciliation_item_line_type", "batch_id", "line_type"),
        Index(
            "pix_reconciliation_item_settled_txn",
            "settled_transaction_id",
            postgresql_where="settled_transaction_id is not null",
        ),
    )

    def acquirer_markup_minor(self) -> int:
        """``fee_minor - interchange_minor - scheme_fee_minor``, never below zero."""
        markup = self.fee_minor - self.interchange_minor - self.scheme_fee_minor
        return max(markup, 0)

    def is_terminal(self) -> bool:
        """Whether nothing will move this item without an operator."""
        return self.status in {"settled", "failed", "orphaned"}

    def compute_variance_minor(self) -> int:
        """Signed difference between what the acquirer settled and what we authorised.

        ``SettlementPoster`` compares ``abs(variance_minor)`` against
        ``merchant.settlement_tolerance_minor`` **before** anything posts: acquirers
        routinely settle a different amount than authorised (partial capture,
        interchange downgrade, DCC, file error), and a wrong file silently accepted
        credits the merchant the wrong amount while the ledger stays internally balanced.
        """
        if self.expected_gross_minor is None:
            return 0
        return self.gross_minor - self.expected_gross_minor
