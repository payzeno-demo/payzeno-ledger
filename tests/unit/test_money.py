"""Money primitives — app/domain/money.py.

The exponent table is NOT ours. It is imported from payzeno_contracts so that the ledger,
payzeno-api and the console cannot disagree about what "100" means in JPY. Half of this
module exists to assert that we did not quietly re-declare it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from payzeno_contracts.types import CURRENCIES, CURRENCY_EXPONENT

from app.domain.money import (
    Money,
    add_money,
    allocate_remainder,
    assert_same_currency,
    format_money,
    from_minor,
    sub_money,
    to_minor,
)
from app.errors import CurrencyMismatchError, NegativeAmountError


def test_money_is_frozen() -> None:
    m = Money(amount_minor=1000, currency="USD")
    with pytest.raises(AttributeError):
        m.amount_minor = 2000  # type: ignore[misc]


def test_currency_exponent_comes_from_contracts() -> None:
    # If someone adds a local table this test is the tripwire. See interfaces.md §3.1.
    assert CURRENCY_EXPONENT["USD"] == 2
    assert CURRENCY_EXPONENT["EUR"] == 2
    assert CURRENCY_EXPONENT["GBP"] == 2
    assert CURRENCY_EXPONENT["JPY"] == 0
    assert set(CURRENCY_EXPONENT) == set(CURRENCIES)


@pytest.mark.parametrize(
    ("major", "currency", "expected_minor"),
    [
        (Decimal("100.00"), "USD", 10_000),
        (Decimal("0.01"), "USD", 1),
        (Decimal("1234.56"), "EUR", 123_456),
        (Decimal("99.99"), "GBP", 9_999),
        # exponent 0 — 1500 yen is 1500 minor units, not 150000
        (Decimal("1500"), "JPY", 1_500),
        (Decimal("0"), "JPY", 0),
    ],
)
def test_to_minor(major: Decimal, currency: str, expected_minor: int) -> None:
    assert to_minor(major, currency) == expected_minor


@pytest.mark.parametrize(
    ("minor", "currency", "expected_major"),
    [
        (10_000, "USD", Decimal("100.00")),
        (1, "USD", Decimal("0.01")),
        (1_500, "JPY", Decimal("1500")),
    ],
)
def test_from_minor(minor: int, currency: str, expected_major: Decimal) -> None:
    assert from_minor(minor, currency) == expected_major


def test_to_minor_round_trips_through_from_minor() -> None:
    for currency in ("USD", "EUR", "GBP", "JPY"):
        for minor in (0, 1, 7, 999, 123_456_789):
            assert to_minor(from_minor(minor, currency), currency) == minor


def test_to_minor_rejects_more_precision_than_the_currency_has() -> None:
    # 0.005 USD is not a representable amount. Quietly rounding it is how a ledger
    # acquires a half-cent it can never balance.
    with pytest.raises(ValueError, match="precision"):
        to_minor(Decimal("0.005"), "USD")
    with pytest.raises(ValueError, match="precision"):
        to_minor(Decimal("1500.5"), "JPY")


@pytest.mark.parametrize(
    ("money", "expected"),
    [
        (Money(amount_minor=10_000, currency="USD"), "100.00 USD"),
        (Money(amount_minor=-2_550, currency="USD"), "-25.50 USD"),
        (Money(amount_minor=1_500, currency="JPY"), "1500 JPY"),
        (Money(amount_minor=0, currency="EUR"), "0.00 EUR"),
    ],
)
def test_format_money(money: Money, expected: str) -> None:
    assert format_money(money) == expected


def test_add_and_sub_money() -> None:
    a = Money(amount_minor=10_000, currency="USD")
    b = Money(amount_minor=2_550, currency="USD")

    assert add_money(a, b) == Money(amount_minor=12_550, currency="USD")
    assert sub_money(a, b) == Money(amount_minor=7_450, currency="USD")


def test_sub_money_may_go_negative() -> None:
    # A merchant payable balance legitimately goes negative — refunds and chargebacks on a
    # merchant with no incoming volume. domain-model.md §9.
    a = Money(amount_minor=1_000, currency="USD")
    b = Money(amount_minor=4_000, currency="USD")
    assert sub_money(a, b).amount_minor == -3_000


def test_arithmetic_across_currencies_raises() -> None:
    usd = Money(amount_minor=10_000, currency="USD")
    eur = Money(amount_minor=10_000, currency="EUR")

    with pytest.raises(CurrencyMismatchError):
        add_money(usd, eur)
    with pytest.raises(CurrencyMismatchError):
        sub_money(usd, eur)
    with pytest.raises(CurrencyMismatchError):
        assert_same_currency(usd, eur)


def test_assert_same_currency_is_a_no_op_when_they_match() -> None:
    usd = Money(amount_minor=1, currency="USD")
    assert assert_same_currency(usd, Money(amount_minor=2, currency="USD")) is None


def test_money_rejects_unknown_currency() -> None:
    with pytest.raises(ValueError, match="unknown currency"):
        Money(amount_minor=1, currency="XYZ")


@pytest.mark.parametrize(
    ("total", "weights", "expected"),
    [
        (100, [1, 1, 1], [34, 33, 33]),
        (10, [1, 1], [5, 5]),
        (7, [3, 1], [6, 1]),
        (0, [1, 1], [0, 0]),
    ],
)
def test_allocate_remainder_is_largest_remainder_and_conserves_the_total(
    total: int, weights: list[int], expected: list[int]
) -> None:
    # Written for the FX work that never shipped. Still the reference implementation the
    # fee apportionment in app/domain/fees.py delegates to, so it stays tested.
    allocated = allocate_remainder(total, weights)
    assert allocated == expected
    assert sum(allocated) == total


def test_allocate_remainder_rejects_a_negative_total() -> None:
    with pytest.raises(NegativeAmountError):
        allocate_remainder(-1, [1, 1])
