"""`app/domain/fx.py` and the two FX payout posting rules — RFC platform-v2 §7.

The rates below are the sim feed's: `{"EUR": "0.9231", "GBP": "0.7902", "ILS": "3.7120",
"JPY": "151.20", "USD": "1"}`, units per one USD. Every number asserted here is worked out
by hand in the comment beside it, because "the code says so" is not a check on the code.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.fx import (
    convert_minor,
    cross_rate,
    format_rate,
    marked_down_rate,
    parse_rate,
    quote_payout_conversion,
)
from app.domain.money import Money
from app.domain.postings import (
    FxPayoutSourcePostingRule,
    FxPayoutTargetPostingRule,
    PostingContext,
    mirrored,
)
from app.errors import ValidationError
from app.models.account import NORMAL_BALANCE_BY_TYPE, PLATFORM_ACCOUNT_TYPES

RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("0.9231"),
    "GBP": Decimal("0.7902"),
    "ILS": Decimal("3.7120"),
    "JPY": Decimal("151.20"),
}


# -- rates --------------------------------------------------------------------------------


def test_cross_rate_goes_through_the_base() -> None:
    # USD per EUR = 1 / 0.9231 = 1.08330625067706640667...
    assert cross_rate(RATES, "EUR", "USD") == Decimal("1.0833062507")
    # JPY per EUR = 151.20 / 0.9231 = 163.79590510237...
    assert cross_rate(RATES, "EUR", "JPY") == Decimal("163.7959051024")
    assert cross_rate(RATES, "USD", "GBP") == Decimal("0.7902000000")


def test_cross_rate_of_a_currency_with_itself_is_one() -> None:
    assert cross_rate(RATES, "ILS", "ILS") == Decimal(1)


def test_cross_rate_needs_both_currencies_quoted() -> None:
    with pytest.raises(ValidationError):
        cross_rate({"USD": Decimal(1)}, "EUR", "USD")


def test_cross_rate_refuses_an_unsupported_currency() -> None:
    with pytest.raises(ValidationError):
        cross_rate({**RATES, "CHF": Decimal("0.88")}, "CHF", "USD")


def test_parse_rate_takes_decimal_strings() -> None:
    assert parse_rate("0.9231", currency="EUR") == Decimal("0.9231")
    assert parse_rate(Decimal("151.20"), currency="JPY") == Decimal("151.20")


@pytest.mark.parametrize("raw", [0.9231, True, "abc", "0", "-1.2", "NaN"])
def test_parse_rate_refuses_floats_and_non_positive_values(raw: object) -> None:
    with pytest.raises(ValidationError):
        parse_rate(raw, currency="EUR")


def test_the_payout_markup_comes_off_the_rate() -> None:
    # 1.0833062507 x 0.99 = 1.072473188193 -> 1.0724731882 at ten places
    assert marked_down_rate(Decimal("1.0833062507"), 100) == Decimal("1.0724731882")
    assert marked_down_rate(Decimal("1.5"), 0) == Decimal("1.5")


@pytest.mark.parametrize("bps", [-1, 10_000])
def test_a_markup_outside_zero_to_hundred_percent_is_refused(bps: int) -> None:
    with pytest.raises(ValidationError):
        marked_down_rate(Decimal("1.1"), bps)


def test_format_rate_is_plain_decimal() -> None:
    assert format_rate(Decimal("1.0724731882")) == "1.0724731882"
    assert format_rate(Decimal("1.0700000000")) == "1.07"
    assert format_rate(Decimal("163.0000000000")) == "163"


# -- amounts and exponents ----------------------------------------------------------------


def test_convert_minor_respects_jpy_having_no_minor_unit() -> None:
    # USD 10.00 at 151.20 = JPY 1512
    assert convert_minor(1000, "USD", "JPY", Decimal("151.20")) == 1512
    # JPY 1512 at 1/151.20 = USD 10.00
    assert convert_minor(1512, "JPY", "USD", Decimal("0.0066137566")) == 1000


def test_convert_minor_rounds_half_up_at_the_target_exponent() -> None:
    # 1 cent x 0.5 = 0.5 cent -> 1 (half up, never to even)
    assert convert_minor(1, "EUR", "USD", Decimal("0.5")) == 1
    # 3 cents x 0.5 = 1.5 -> 2
    assert convert_minor(3, "EUR", "USD", Decimal("0.5")) == 2


def test_a_eur_payout_to_a_usd_account() -> None:
    """EUR 100.00 at mid 1.0833062507, 100 bps markup.

    rate   = 1.0724731882
    payout = 10000 x 1.0724731882 = 10724.73... -> USD 107.25
    mid    = 10000 x 1.0833062507 = 10833.06... -> USD 108.33
    spread = 108.33 - 107.25 = USD 1.08
    """
    conversion = quote_payout_conversion(
        Money(10_000, "EUR"), "USD", mid_rate=Decimal("1.0833062507"), markup_bps=100
    )

    assert conversion.target == Money(10_725, "USD")
    assert conversion.mid_target_minor == 10_833
    assert conversion.spread_minor == 108
    assert conversion.rate == Decimal("1.0724731882")
    assert conversion.source == Money(10_000, "EUR")


def test_a_usd_payout_to_a_jpy_account() -> None:
    """USD 10.00 at 151.20 with 100 bps: rate 149.688, payout JPY 1497, mid 1512."""
    conversion = quote_payout_conversion(
        Money(1_000, "USD"), "JPY", mid_rate=Decimal("151.2"), markup_bps=100
    )

    assert conversion.target == Money(1_497, "JPY")
    assert conversion.mid_target_minor == 1_512
    assert conversion.spread_minor == 15


def test_the_spread_is_never_negative() -> None:
    for amount in (1, 7, 99, 101, 12_345, 9_999_999):
        conversion = quote_payout_conversion(
            Money(amount, "GBP"), "ILS", mid_rate=Decimal("4.6975449254"), markup_bps=100
        )
        assert conversion.spread_minor >= 0


def test_an_amount_too_small_to_survive_conversion_is_refused() -> None:
    # JPY 1 at 0.004 less 1% is 0.396 of a cent, which rounds to nothing.
    with pytest.raises(ValidationError):
        quote_payout_conversion(Money(1, "JPY"), "USD", mid_rate=Decimal("0.004"), markup_bps=100)


def test_there_is_no_conversion_into_the_same_currency() -> None:
    with pytest.raises(ValidationError):
        quote_payout_conversion(Money(100, "USD"), "USD", mid_rate=Decimal(1), markup_bps=100)


# -- chart of accounts and posting rules --------------------------------------------------


def test_fx_accounts_are_platform_accounts_with_the_right_normal_side() -> None:
    assert NORMAL_BALANCE_BY_TYPE["fx_clearing"] == "debit"
    assert NORMAL_BALANCE_BY_TYPE["fx_revenue"] == "credit"
    assert {"fx_clearing", "fx_revenue"} <= PLATFORM_ACCOUNT_TYPES


def _ctx(currency: str, *, gross: int, net: int, fee: int) -> PostingContext:
    return PostingContext(
        merchant_id="mer_fx_1",
        currency=currency,
        livemode=True,
        gross_minor=gross,
        fee_minor=fee,
        net_minor=net,
        interchange_minor=0,
        scheme_fee_minor=0,
        reserve_bps=0,
        platform_fee_bps=0,
        platform_fee_fixed_minor=0,
    )


def _signed(lines) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in lines:
        out[line.account_type] = out.get(line.account_type, 0) + line.signed_minor()
    return out


def test_the_source_leg_moves_the_balance_into_fx_clearing() -> None:
    lines = FxPayoutSourcePostingRule().build(_ctx("EUR", gross=10_000, net=10_000, fee=0))

    assert _signed(lines) == {"merchant_payable": 10_000, "fx_clearing": -10_000}


def test_the_target_leg_pays_cash_and_books_the_spread() -> None:
    lines = FxPayoutTargetPostingRule().build(_ctx("USD", gross=10_833, net=10_725, fee=108))

    assert _signed(lines) == {"fx_clearing": 10_833, "cash": -10_725, "fx_revenue": -108}


def test_a_zero_spread_leg_is_dropped_not_posted() -> None:
    lines = FxPayoutTargetPostingRule().build(_ctx("USD", gross=500, net=500, fee=0))

    assert {line.account_type for line in lines} == {"fx_clearing", "cash"}


def test_the_target_leg_refuses_amounts_that_do_not_add_up() -> None:
    with pytest.raises(ValidationError):
        FxPayoutTargetPostingRule().build(_ctx("USD", gross=10_833, net=10_725, fee=100))


def test_mirrored_legs_cancel_the_originals() -> None:
    lines = FxPayoutTargetPostingRule().build(_ctx("USD", gross=10_833, net=10_725, fee=108))

    combined = _signed([*lines, *mirrored(lines)])

    assert set(combined.values()) == {0}
