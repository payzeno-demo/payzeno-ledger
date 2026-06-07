"""``funding_event`` — a real bank credit, ingested from the statement feed.

Cash follows the bank, not the file. ``settle`` posts on the strength of the acquirer's
settlement file; ``settlement_funding`` posts only when a row here matches the batch within
``FUNDING_MATCH_TOLERANCE_BPS``. **Nothing else debits ``cash``.**

Without this table the ledger claims cash on the strength of an acquirer file alone, and a
short-paying acquirer leaves Payzeno paying merchants out of money that never arrived —
``PayoutCalculator.compute_available`` counts only *funded* batches for exactly this
reason.

``FundingMatchJob`` (900s) drives it off ``pix_funding_event_unmatched``.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar, Final

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    TimestampMixin,
    acquirer_enum,
    funding_event_status_enum,
)

#: Default match tolerance. `Settings.funding_match_tolerance_bps` overrides it; treasury
#: moved it from 5 to 10 bps in month 11 after Nordpay started rounding at the file level.
DEFAULT_TOLERANCE_BPS: Final[int] = 10


class FundingEvent(Base, TimestampMixin, LivemodeMixin):
    """One credit on Payzeno's bank statement, from one acquirer, on one value date."""

    __tablename__ = "funding_event"
    entity_name: ClassVar[str] = "funding_event"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    acquirer: Mapped[str] = mapped_column(acquirer_enum, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: A banking-calendar date, not an instant — the bank's own value date.
    value_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: The bank's reference for the credit. Unique: the statement feed is at-least-once
    #: and re-ingesting a day's file must not double-fund a batch.
    bank_reference: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        funding_event_status_enum, nullable=False, server_default="unmatched"
    )
    matched_batch_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("settlement_batch.id", ondelete="RESTRICT"), nullable=True
    )
    #: Signed: negative means the acquirer short-paid. `short_paid` events are the ones
    #: finance chases, and they are why `settlement_funding` posts the FUNDED amount and
    #: not the batch's posted total.
    variance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("uq_funding_event_bank_reference", "bank_reference", unique=True),
        Index(
            "pix_funding_event_unmatched",
            "value_date",
            postgresql_where="status = 'unmatched'",
        ),
        Index("ix_funding_event_acquirer_value_date", "acquirer", "value_date"),
    )

    def within_tolerance(self, posted_total_minor: int, tolerance_bps: int) -> bool:
        """Whether this credit matches a batch's posted total closely enough to fund it.

        Invariant 8 of `data-model.md` §6 re-checks this nightly: every batch in
        ``funded`` has a matched funding event inside the tolerance.
        """
        if posted_total_minor <= 0:
            return False
        drift = abs(self.amount_minor - posted_total_minor)
        allowed = (posted_total_minor * tolerance_bps) // 10_000
        return drift <= allowed

    def is_short_paid(self, posted_total_minor: int) -> bool:
        """Whether the acquirer paid less than the batch says they owe."""
        return self.amount_minor < posted_total_minor

    def is_matched(self) -> bool:
        """Whether a batch has already claimed this credit."""
        return self.matched_batch_id is not None
