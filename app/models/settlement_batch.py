"""``settlement_batch`` — the unit of money actually arriving from an acquirer.

One batch = one acquirer settlement file = one currency = one calendar processing day.

::

    open ─close─▶ closed ─start_reconcile─▶ reconciling ─▶ reconciled ─fund─▶ funded
                                                 │
                                                 ├─▶ partially_reconciled
                                                 └─▶ failed

``partially_reconciled`` is the state that keeps a retry backlog alive, and is therefore
the state arc INC's race needs. It is reachable and it is normal — two batches sat in it
for six hours on the night of PAY-2041.

``funded`` means a ``funding_event`` — an actual bank credit — matched within
``FUNDING_MATCH_TOLERANCE_BPS`` and ``settlement_funding`` has posted.
``PayoutCalculator.compute_available`` counts **only funded batches**, so a short-paying
acquirer cannot leave Payzeno paying merchants out of money that never arrived.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar, Final

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    TimestampMixin,
    acquirer_enum,
    settlement_batch_status_enum,
)

#: Statuses `ReconciliationSweepJob` picks up. `closed` is a first pass;
#: `partially_reconciled` is a batch with retryable items left over.
SWEEPABLE_STATUSES: Final[tuple[str, ...]] = ("closed", "partially_reconciled")

#: Terminal-ish: nothing sweeps these again without an operator.
CLOSED_STATUSES: Final[frozenset[str]] = frozenset({"funded", "failed"})


class SettlementBatch(Base, TimestampMixin, LivemodeMixin):
    """One acquirer settlement file, and its reconciliation lifecycle."""

    __tablename__ = "settlement_batch"
    entity_name: ClassVar[str] = "settlement_batch"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    processing_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: The acquirer's own file identifier. Half of `uq_settlement_batch_file`, and what
    #: `ProcessorClient.confirm_settlement` sends back on every item.
    file_reference: Mapped[str] = mapped_column(Text, nullable=False)

    #: The sum of settled items' `net_minor`. Invariant 5 of `data-model.md` §6 compares
    #: the two; PAY-2055's second alarm fires on posted/expected > 1.001, which is what
    #: nobody had on the night of the incident.
    posted_total_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    funded_amount_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(
        settlement_batch_status_enum, nullable=False, server_default="open"
    )

    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    funded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # An acquirer re-filing the same reference is an at-least-once delivery, not a
        # second batch. SettlementImportService leans on this to be idempotent.
        Index("uq_settlement_batch_file", "acquirer", "file_reference", unique=True),
        Index(
            "ix_settlement_batch_status_date",
            "status",
            "processing_date",
            postgresql_ops={"processing_date": "DESC"},
        ),
        # Migration 0021, arc PERF: the monthly finance export filtered by currency and
        # date and was using the status index, then throwing away 90% of the rows.
        Index(
            "ix_settlement_batch_currency_date",
            "currency",
            "processing_date",
            postgresql_ops={"processing_date": "DESC"},
        ),
        # FundingMatchJob's driving index — reconciled batches still waiting for cash.
        Index(
            "pix_settlement_batch_unfunded",
            "processing_date",
            postgresql_where="status = 'reconciled' and funded_at is null",
        ),
    )

    def is_sweepable(self) -> bool:
        """Whether ``ReconciliationSweepJob`` should pick this batch up."""
        return self.status in SWEEPABLE_STATUSES

    def is_reconcilable(self) -> bool:
        """Whether a reconciliation run may start.

        ``SettlementService.close_batch`` raises :class:`BatchNotReconcilableError` for a
        batch that is not ``open``; this is the mirror check on the run side.
        """
        return self.status in {"closed", "reconciling", "partially_reconciled"}

    def overposted_ratio(self) -> float:
        """``posted_total_minor / expected_total_minor``, or 0.0 when nothing is expected.

        PAY-2055's ``ledger_batch_overposted`` alarm fires above 1.001. On the night of
        PAY-2041 this ratio reached 1.09 on ``sb_…QK`` and nothing was watching it.
        """
        if self.expected_total_minor == 0:
            return 0.0
        return self.posted_total_minor / self.expected_total_minor
