"""Fee computation — app/domain/fees.py.

Two functions with very different jobs:

* `apportion_fee` splits what the ACQUIRER kept (an expense) across interchange, scheme and
  acquirer markup. Largest-remainder, and the remainder lands on the markup component —
  never on `rounding_adjustment`. domain-model.md §0.1.
* `compute_platform_fee` computes what PAYZENO kept (revenue). ROUND_HALF_UP, and it has
  to be exponent-aware or JPY is off by two orders of magnitude.

`legacy_blended_fee` is the bit-for-bit port of the Java `LegacyBlendedFeeCalculator`. It is
deprecated and it is still here because arc MIG's parity test is the only thing that proves
the port did not change anyone's invoice.
"""

from __future__ import annotations

import pytest

from app.domain.fees import apportion_fee, compute_platform_fee, legacy_blended_fee
from app.domain.money import FeeBreakdown, FeeComponent, Money


def _usd(minor: int) -> Money:
    return Money(amount_minor=minor, currency="USD")


def test_apportion_fee_splits_by_weight_and_conserves_the_total() -> None:
    gross = _usd(10_000)
    components = [
        FeeComponent(name="interchange", bps=150, fixed_minor=10),
        FeeComponent(name="scheme_fee", bps=13, fixed_minor=0),
        FeeComponent(name="acquirer_markup", bps=30, fixed_minor=5),
    ]

    breakdown = apportion_fee(gross, components)

    assert isinstance(breakdown, FeeBreakdown)
    assert set(breakdown.components) == {"interchange", "scheme_fee", "acquirer_markup"}
    total = sum(m.amount_minor for m in breakdown.components.values())
    assert total == breakdown.total.amount_minor


def test_apportion_fee_puts_the_remainder_on_the_markup_not_on_rounding() -> None:
    # Three components, a total that does not divide evenly. The odd minor unit belongs to
    # the acquirer markup, because that is the only component whose value is derived rather
    # than published. Sending it to `rounding_adjustment` makes the trial balance report a
    # rounding artefact on every single settled charge.
    gross = _usd(1_000)
    components = [
        FeeComponent(name="interchange", bps=100, fixed_minor=0),
        FeeComponent(name="scheme_fee", bps=100, fixed_minor=0),
        FeeComponent(name="acquirer_markup", bps=101, fixed_minor=0),
    ]

    breakdown = apportion_fee(gross, components)

    assert breakdown.remainder_minor == 0
    assert breakdown.components["acquirer_markup"].amount_minor >= (
        breakdown.components["interchange"].amount_minor
    )


def test_apportion_fee_with_a_single_component_returns_the_whole_fee() -> None:
    breakdown = apportion_fee(_usd(10_000), [FeeComponent(name="blended", bps=290, fixed_minor=30)])
    assert breakdown.components["blended"] == breakdown.total


def test_apportion_fee_with_no_components_is_zero() -> None:
    breakdown = apportion_fee(_usd(10_000), [])
    assert breakdown.total.amount_minor == 0
    assert breakdown.components == {}


@pytest.mark.parametrize(
    ("gross_minor", "bps", "fixed_minor", "expected_minor"),
    [
        # 2.9% + 30c on $100.00 -> 320
        (10_000, 290, 30, 320),
        # exact half rounds UP, not to even. 0.5 -> 1.
        (100, 50, 0, 1),
        (300, 50, 0, 2),
        # zero-rated merchants exist (internal test accounts)
        (10_000, 0, 0, 0),
        (10_000, 0, 25, 25),
    ],
)
def test_compute_platform_fee_rounds_half_up(
    gross_minor: int, bps: int, fixed_minor: int, expected_minor: int
) -> None:
    fee = compute_platform_fee(_usd(gross_minor), bps=bps, fixed_minor=fixed_minor)
    assert fee.amount_minor == expected_minor
    assert fee.currency == "USD"


def test_compute_platform_fee_is_exponent_aware_for_jpy() -> None:
    # 2.9% + 30 yen on 10,000 yen. The fixed component is 30 MINOR units, and JPY's
    # exponent is 0, so that is 30 yen — not 0.30.
    fee = compute_platform_fee(Money(amount_minor=10_000, currency="JPY"), bps=290, fixed_minor=30)
    assert fee == Money(amount_minor=320, currency="JPY")


def test_compute_platform_fee_never_exceeds_the_gross() -> None:
    fee = compute_platform_fee(_usd(100), bps=20_000, fixed_minor=0)
    assert fee.amount_minor <= 100


def test_legacy_blended_fee_matches_the_java_calculator() -> None:
    """Parity vectors lifted from LegacyBlendedFeeCalculatorTest in payzeno-billing-legacy.

    Deprecated since month 4 and still imported by exactly one branch of `apportion_fee`
    (blended-pricing merchants) plus this test. Do not "modernise" the rounding: the whole
    point is that it is wrong in the same direction the Java was wrong in.
    """
    assert legacy_blended_fee(gross_minor=10_000, bps=290).amount_minor == 290
    assert legacy_blended_fee(gross_minor=333, bps=290).amount_minor == 9
    assert legacy_blended_fee(gross_minor=1, bps=290).amount_minor == 0
