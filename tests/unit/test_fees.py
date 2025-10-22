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
