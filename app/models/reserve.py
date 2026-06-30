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
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: The capture that created the hold. Not nullable: a reserve tranche with no source
    #: transaction is unauditable, and the nightly trial balance has no way to prove the
    #: `reserve` account's balance is the sum of live holds.
    held_from_transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=False
    )
    #: A banking-calendar date. `ReserveReleaseJob` runs daily and picks up everything due.
    release_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: Null until released. The partial index below is keyed on exactly this.
    released_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        Index(
            "pix_reserve_hold_due",
            "release_on",
            postgresql_where="released_transaction_id is null",
        ),
        Index("ix_reserve_hold_merchant", "merchant_id", "currency"),
    )

    def is_released(self) -> bool:
        """Whether the ``reserve_release`` posting has already happened."""
        return self.released_transaction_id is not None

    def is_due(self, on: dt.date) -> bool:
        """Whether ``ReserveReleaseJob`` should release this hold today."""
        return not self.is_released() and self.release_on <= on


class BankingCalendarDay(Base):
    """One (currency, rail, date) row of the banking calendar.

    Loaded a year at a time by ``BankingCalendarRepository.load_window`` and handed to
    :meth:`app.domain.calendar.BankingCalendar.from_rows`, which duck-types it — the pure
    domain layer may not import this module.
    """

    __tablename__ = "banking_calendar"
    entity_name: ClassVar[str] = "banking_calendar"

    currency: Mapped[str] = mapped_column(Currency, primary_key=True)
    rail: Mapped[str] = mapped_column(payout_method_enum, primary_key=True)
    calendar_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    is_business_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
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

    def is_holiday(self) -> bool:
        """A named closure, as opposed to an ordinary weekend."""
        return not self.is_business_day and self.holiday_name is not None
