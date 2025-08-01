"""`BankingCalendar` — app/domain/calendar.py.

Backed by the `banking_calendar` table, keyed (currency, rail, calendar_date). The point of
the table — and of this test module — is that SEPA, Faster Payments and the two ACH rails
share neither holidays nor cutoffs, which is why there is no single PAYOUT_CUTOFF_HOUR_UTC
and why `app/config.py` carries four per-rail cutoffs instead.

The calendar rows are injected, so this stays a pure-domain test.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from app.domain.calendar import BankingCalendar

# 2026-07-03 is a Friday. 2026-07-04 (Independence Day, observed Friday) is a US bank
# holiday for both ACH rails and a perfectly ordinary business day for SEPA and FPS.
CALENDAR_ROWS = {
    ("USD", "standard_ach", date(2026, 7, 3)): (True, None),
    ("USD", "standard_ach", date(2026, 7, 4)): (False, "Independence Day"),
    ("USD", "standard_ach", date(2026, 7, 5)): (False, "weekend"),
    ("USD", "standard_ach", date(2026, 7, 6)): (True, None),
    ("USD", "same_day_ach", date(2026, 7, 3)): (True, None),
    ("USD", "same_day_ach", date(2026, 7, 6)): (True, None),
    ("EUR", "sepa", date(2026, 7, 3)): (True, None),
    ("EUR", "sepa", date(2026, 7, 4)): (False, "weekend"),
    ("EUR", "sepa", date(2026, 7, 5)): (False, "weekend"),
    ("EUR", "sepa", date(2026, 7, 6)): (True, None),
    ("GBP", "faster_payments", date(2026, 7, 3)): (True, None),
    ("GBP", "faster_payments", date(2026, 8, 31)): (False, "Summer Bank Holiday"),
    ("GBP", "faster_payments", date(2026, 9, 1)): (True, None),
}


@pytest.fixture
def calendar() -> BankingCalendar:
    return BankingCalendar(
        days=CALENDAR_ROWS,
        cutoffs={
            "standard_ach": time(21, 0),
            "same_day_ach": time(16, 45),
            "sepa": time(14, 0),
            "faster_payments": time(17, 30),
        },
    )


def test_is_business_day_reads_the_row(calendar: BankingCalendar) -> None:
    assert calendar.is_business_day(date(2026, 7, 3), "USD", "standard_ach") is True
    assert calendar.is_business_day(date(2026, 7, 4), "USD", "standard_ach") is False


def test_next_business_day_skips_a_holiday_and_the_weekend(calendar: BankingCalendar) -> None:
    # Friday the 4th is the holiday, the 5th is Sunday -> Monday the 6th.
    assert calendar.next_business_day(date(2026, 7, 4), "USD", "standard_ach") == date(2026, 7, 6)


def test_next_business_day_returns_the_day_itself_when_it_is_already_open(
    calendar: BankingCalendar,
) -> None:
    assert calendar.next_business_day(date(2026, 7, 3), "USD", "standard_ach") == date(2026, 7, 3)


def test_the_same_date_differs_per_rail(calendar: BankingCalendar) -> None:
    """The reason `banking_calendar` is keyed on the rail and not only the currency.

    4 July closes both US ACH rails. It does nothing at all to SEPA. A single shared
    calendar pushes every euro payout out by a day for a US holiday.
    """
    assert calendar.is_business_day(date(2026, 7, 4), "USD", "standard_ach") is False
    assert calendar.is_business_day(date(2026, 7, 3), "EUR", "sepa") is True


def test_faster_payments_has_its_own_holidays(calendar: BankingCalendar) -> None:
    assert calendar.is_business_day(date(2026, 8, 31), "GBP", "faster_payments") is False
    assert calendar.next_business_day(
        date(2026, 8, 31), "GBP", "faster_payments"
    ) == date(2026, 9, 1)


@pytest.mark.parametrize(
    ("rail", "expected"),
    [
        ("standard_ach", time(21, 0)),
        ("same_day_ach", time(16, 45)),
        ("sepa", time(14, 0)),
        ("faster_payments", time(17, 30)),
    ],
)
def test_cutoff_utc_is_per_rail(calendar: BankingCalendar, rail: str, expected: time) -> None:
    assert calendar.cutoff_utc(rail) == expected  # type: ignore[arg-type]


def test_cutoffs_are_all_distinct(calendar: BankingCalendar) -> None:
    cutoffs = {
        calendar.cutoff_utc(rail)  # type: ignore[arg-type]
        for rail in ("standard_ach", "same_day_ach", "sepa", "faster_payments")
    }
    assert len(cutoffs) == 4


def test_an_unknown_day_falls_back_to_the_weekday_rule(calendar: BankingCalendar) -> None:
    # The table is loaded a year at a time. A date past the loaded horizon must not crash a
    # payout; it degrades to Mon-Fri and the next load corrects it.
    assert calendar.is_business_day(date(2030, 1, 2), "USD", "standard_ach") is True
    assert calendar.is_business_day(date(2030, 1, 5), "USD", "standard_ach") is False


def test_next_business_day_terminates_on_a_fully_closed_window(calendar: BankingCalendar) -> None:
    closed = BankingCalendar(
        days={("USD", "standard_ach", date(2026, 7, d)): (False, "x") for d in range(1, 32)},
        cutoffs={"standard_ach": time(21, 0)},
    )
    with pytest.raises(ValueError, match="no business day"):
        closed.next_business_day(date(2026, 7, 1), "USD", "standard_ach")
