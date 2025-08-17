"""``reserve_hold`` and ``banking_calendar``.

**Reserve is released, not accumulated.** ``CapturePostingRule`` credits ``reserve`` when
``merchant.reserve_bps > 0`` and the caller writes a :class:`ReserveHold` row with
``release_on = capture_date + merchant.reserve_hold_days``. ``ReserveReleaseJob`` posts
``reserve_release`` on that date off ``pix_reserve_hold_due``. Without it a merchant on a
10% rolling reserve accumulates money Payzeno can never pay out.

:class:`BankingCalendarDay` backs ``app/domain/calendar.py::BankingCalendar``. SEPA,
Faster Payments and the two ACH rails are in different jurisdictions and share neither
holidays nor cutoffs, which is why the primary key is ``(currency, rail, calendar_date)``
and why there is no single ``PAYOUT_CUTOFF_HOUR_UTC``.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    TimestampMixin,
    payout_method_enum,
)


class ReserveHold(Base, TimestampMixin, LivemodeMixin):
    """One tranche of rolling reserve, withheld at capture and released on a date."""

    __tablename__ = "reserve_hold"
    entity_name: ClassVar[str] = "reserve_hold"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: The capture that created the hold. Not nullable: a reserve tranche with no source
    #: transaction is unauditable, and the nightly trial balance has no way to prove the
    #: `reserve` account's balance is the sum of live holds.
    held_from_transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=False
    )
    __tablename__ = "banking_calendar"
    rail: Mapped[str] = mapped_column(payout_method_enum, primary_key=True)
    calendar_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    #: "Thanksgiving", "TARGET2 closure", "Spring bank holiday". Surfaced in the payouts
    #: runbook so an operator can answer "why is this payout a day late" without guessing.
    holiday_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "pix_banking_calendar_holidays",
            "currency",
            "rail",
            "calendar_date",
            postgresql_where="not is_business_day",
        ),
    )

