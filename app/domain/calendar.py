"""Banking calendar — funds availability, per currency and per rail.

`domain-model.md` §9. ``available_on`` is computed, not guessed::

    available_on = BankingCalendar.next_business_day(
        settlement_date + merchant.payout_delay_days, currency, rail)

SEPA, Faster Payments and the two ACH rails are in different jurisdictions and share
neither holidays nor cutoffs, which is why there is no single ``PAYOUT_CUTOFF_HOUR_UTC``
and why ``app/config.py`` carries four per-rail cutoffs.

Layer L0: this module holds no session and imports no model. ``PayoutService`` loads
``banking_calendar`` rows through ``BankingCalendarRepository`` and hands them to
:meth:`BankingCalendar.from_rows`, which duck-types whatever it is given.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any, Final

from payzeno_contracts.types import PayoutMethod

from app.errors import ValidationError

#: Per-rail submission cutoff, UTC. Defaults; `app/config.py::Settings` overrides each one
#: with `PAYOUT_CUTOFF_*_UTC` so treasury can move a cutoff without a code change.
DEFAULT_CUTOFFS: Final[dict[str, time]] = {
    "standard_ach": time(21, 0),
    "same_day_ach": time(15, 45),
    "sepa": time(15, 0),
    "faster_payments": time(17, 30),
    "debit_ach": time(21, 0),
}

#: Saturday, Sunday. Every supported rail is closed on both; ILS settlement is handled by
#: explicit `banking_calendar` rows rather than by a second weekend definition, because
#: Payzeno pays ILS out over SEPA-correspondent rails, not over a domestic Israeli rail.
_WEEKEND: Final[frozenset[int]] = frozenset({5, 6})

#: Refuse to walk forever if a caller hands us a calendar with no business days at all.
_MAX_LOOKAHEAD_DAYS: Final[int] = 30


@dataclass(frozen=True, slots=True)
class BankingCalendarDayView:
    """One row of ``banking_calendar``, flattened for the pure layer."""

    currency: str
    rail: str
    calendar_date: date
    is_business_day: bool
    holiday_name: str | None = None


class BankingCalendar:
    """Answers "is this a business day" and "when is the next one" per currency and rail.

    Constructed per request from the rows the caller loaded. It is cheap: a payout run
    reads about 400 rows (a year of one currency/rail pair) and keeps them for the pass.

    Days absent from the table fall back to the weekday rule — a calendar that has not
    been loaded past next March must not silently declare every day a holiday and hang
    :meth:`next_business_day` on the lookahead cap.
    """

    def __init__(
        self,
        days: Iterable[BankingCalendarDayView] = (),
        cutoffs: Mapping[str, time] | None = None,
    ) -> None:
        self._days: dict[tuple[str, str, date], BankingCalendarDayView] = {}
        for day in days:
            self._days[(day.currency, day.rail, day.calendar_date)] = day
        # Partial overrides layer over DEFAULT_CUTOFFS rather than replacing it: treasury
        # moves one rail at a time, and a caller that passes {"sepa": ...} must not
        # silently lose the other three. The four `PAYOUT_CUTOFF_*_UTC` env vars arrive
        # here through `Settings.payout_cutoffs`.
        self._cutoffs: dict[str, time] = {**DEFAULT_CUTOFFS, **(cutoffs or {})}

    @classmethod
    def from_rows(cls, rows: Iterable[Any]) -> BankingCalendar:
        """Build from ``banking_calendar`` ORM rows without importing the model.

        Duck-typed on purpose (ADR 0002): ``app/domain`` may not import ``app/models``.
        """
        return cls(
            BankingCalendarDayView(
                currency=str(row.currency).strip(),
                rail=str(row.rail),
                calendar_date=row.calendar_date,
                is_business_day=bool(row.is_business_day),
                holiday_name=getattr(row, "holiday_name", None),
            )
            for row in rows
        )

    def is_business_day(self, day: date, currency: str, rail: PayoutMethod) -> bool:
        """True when `rail` settles `currency` on `day`."""
        known = self._days.get((currency, rail, day))
        if known is not None:
            return known.is_business_day
        return day.weekday() not in _WEEKEND

    def holiday_name(self, day: date, currency: str, rail: PayoutMethod) -> str | None:
        """The holiday that closes `day`, when the calendar names one."""
        known = self._days.get((currency, rail, day))
        return known.holiday_name if known is not None else None

    def next_business_day(self, day: date, currency: str, rail: PayoutMethod) -> date:
        """The first business day on or after `day`.

        Inclusive: a payout whose availability lands on an open Tuesday is available that
        Tuesday, not the Wednesday.
        """
        candidate = day
        for _ in range(_MAX_LOOKAHEAD_DAYS + 1):
            if self.is_business_day(candidate, currency, rail):
                return candidate
            candidate = candidate + timedelta(days=1)
        raise ValidationError(
            "no business day found within the lookahead window",
            details={
                "from": day.isoformat(),
                "currency": currency,
                "rail": rail,
                "lookahead_days": _MAX_LOOKAHEAD_DAYS,
            },
        )

    def add_business_days(
        self, day: date, count: int, currency: str, rail: PayoutMethod
    ) -> date:
        """Advance `count` business days from `day`.

        ``count == 0`` is the same question as :meth:`next_business_day`. Used for
        ``arrival_estimate``: standard ACH is T+2 business days after initiation, SEPA is
        T+1, Faster Payments is same-day.
        """
        if count < 0:
            raise ValidationError("count must not be negative", details={"count": count})
        cursor = self.next_business_day(day, currency, rail)
        for _ in range(count):
            cursor = self.next_business_day(cursor + timedelta(days=1), currency, rail)
        return cursor

    def cutoff_utc(self, rail: PayoutMethod) -> time:
        """Submission cutoff for `rail`, UTC.

        ``same_day_ach`` is the tight one — 15:45 UTC, and it is a hard bank cutoff, not
        an advisory one. ``SameDayAchPayoutInitiator`` refuses to submit past it and
        reschedules to the next business day.
        """
        cutoff = self._cutoffs.get(rail)
        if cutoff is None:
            raise ValidationError("unknown payout rail", details={"rail": rail})
        return cutoff

    def business_days_between(
        self, start: date, end: date, currency: str, rail: PayoutMethod
    ) -> int:
        """Count business days in ``[start, end)``. Used by the reserve ageing report."""
        if end < start:
            raise ValidationError(
                "end precedes start",
                details={"start": start.isoformat(), "end": end.isoformat()},
            )
        count = 0
        cursor = start
        while cursor < end:
            if self.is_business_day(cursor, currency, rail):
                count += 1
            cursor = cursor + timedelta(days=1)
        return count
