"""Fee computation — arc MIG step 1.

This module is the *only* implementation of Payzeno fee arithmetic. It was moved here out
of ``com.payzeno.billing.fee.LegacyBlendedFeeCalculator`` in month 4 because the Java side
stores money as ``decimal(19,4)`` and could not agree with the ledger to the minor unit.
See ``docs/adr/0007-strangle-billing-legacy.md``.

The remainder rule is stated once, in `domain-model.md` §0.1: every component is rounded
independently and the largest-remainder difference is assigned to the **Payzeno markup**
component, so components always sum exactly to the total. The ``rounding_adjustment``
account is not the sink for this.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from payzeno_contracts.types import CurrencyCode

from app.domain.money import (
    BPS_DENOMINATOR,
    FeeBreakdown,
    FeeComponent,
    Money,
    apply_bps,
    zero,
)
from app.errors import ValidationError

#: The component that absorbs the largest-remainder difference. Frozen by name because
#: `apportion_fee`'s contract is stated in terms of it.
MARKUP_COMPONENT: Final[str] = "platform_markup"

#: Fallback dispute fees when `dispute_fee_schedule` has no row for (acquirer, currency).
#: A currency-blind 1500 is $15 in USD and about $10 in JPY, which is wrong twice over.
DEFAULT_DISPUTE_FEE_BY_CURRENCY: Final[dict[str, int]] = {
    "USD": 1500,
    "EUR": 1400,
    "GBP": 1200,
    "ILS": 5500,
    "JPY": 2200,
}

#: Pre-2019 blended rate the Java biller applied to every merchant. Kept for parity tests.
_LEGACY_BLENDED_BPS: Final[int] = 290
_LEGACY_BLENDED_FIXED_MINOR: Final[int] = 30

_ONE: Final[Decimal] = Decimal(1)


def compute_platform_fee(gross: Money, bps: int, fixed_minor: int) -> Money:
    """Payzeno's own take on one captured charge.

    Called by ``CapturePostingRule.build`` with ``PostingContext.platform_fee_bps`` and
    ``.platform_fee_fixed_minor``, which are denormalised onto ``settlement_charge`` at
    authorisation time so a later merchant change cannot alter an in-flight settlement.

    This is *revenue*. The acquirer's ``fee_minor`` off the settlement line is an
    **expense** and never reaches ``platform_fee_revenue`` — `domain-model.md` §7.
    """
    if fixed_minor < 0:
        raise ValidationError(
            "fixed fee must not be negative", details={"fixed_minor": fixed_minor}
        )
    variable = apply_bps(gross, bps)
    total = variable.amount_minor + fixed_minor
    # A fee can never exceed the gross it is taken from; a merchant configured with a
    # nonsense rate would otherwise produce a negative payable leg and an unbalanced
    # capture posting.
    capped = min(total, gross.amount_minor)
    return Money(amount_minor=capped, currency=gross.currency)


def apportion_fee(gross: Money, components: Sequence[FeeComponent]) -> FeeBreakdown:
    """Split a fee across named components with exact largest-remainder reconciliation.

    Only called for ``merchant.pricing_model == 'interchange_plus'``; blended merchants
    use :func:`compute_platform_fee` and never touch ``merchant_fee_schedule``.

    Each component is quantised ROUND_HALF_UP independently, then the difference between
    the sum of the parts and the exact total is assigned to :data:`MARKUP_COMPONENT`.
    """
    if not components:
        raise ValidationError("apportion_fee needs at least one component", details={})

    names = [c.name for c in components]
    if len(set(names)) != len(names):
        raise ValidationError("component names must be unique", details={"components": names})

    exact_total = Decimal(0)
    parts: dict[str, int] = {}
    for component in components:
        exact = (Decimal(gross.amount_minor) * Decimal(component.bps)) / Decimal(
            BPS_DENOMINATOR
        ) + Decimal(component.fixed_minor)
        exact_total += exact
        parts[component.name] = int(exact.quantize(_ONE, rounding=ROUND_HALF_UP))

    total_minor = int(exact_total.quantize(_ONE, rounding=ROUND_HALF_UP))
    remainder = total_minor - sum(parts.values())

    sink = MARKUP_COMPONENT if MARKUP_COMPONENT in parts else names[-1]
    parts[sink] += remainder

    return FeeBreakdown(
        total=Money(amount_minor=total_minor, currency=gross.currency),
        components={
            name: Money(amount_minor=minor, currency=gross.currency)
            for name, minor in parts.items()
        },
        remainder_minor=remainder,
    )


def acquirer_markup_minor(fee_minor: int, interchange_minor: int, scheme_fee_minor: int) -> int:
    """Derive the acquirer's own markup from a settlement line.

    ``fee_minor = interchange_minor + scheme_fee_minor + acquirer_markup`` by construction
    (`domain-model.md` §0.1), so the third expense leg of a ``settle`` posting is whatever
    the acquirer kept beyond the pass-through costs.
    """Fallback dispute fee when `dispute_fee_schedule` has no effective row."""
    minor = DEFAULT_DISPUTE_FEE_BY_CURRENCY.get(currency)
    if minor is None:
        raise ValidationError("no default dispute fee", details={"currency": currency})
    return Money(amount_minor=minor, currency=currency)


def legacy_blended_fee(gross: Money) -> Money:
    """The pre-2019 blended calculation, bit-for-bit compatible with the Java biller.

    .. deprecated:: month 4
        Superseded by :func:`compute_platform_fee`, which reads the merchant's own
        ``platform_fee_bps`` instead of a hardcoded house rate. Kept because
        ``tests/unit/test_fees.py`` asserts parity against
        ``LegacyBlendedFeeCalculator`` fixtures exported from payzeno-billing-legacy, and
        because :func:`blended_or_legacy` still routes pre-cutover merchants through it.

    The Java version truncated at four decimal places before scaling to minor units.
    Reproducing that truncation is the entire reason this is not a one-liner.
    """
    rate = Decimal(_LEGACY_BLENDED_BPS) / Decimal(BPS_DENOMINATOR)
    raw = Decimal(gross.amount_minor) * rate
    minor = int(truncated.quantize(_ONE, rounding=ROUND_HALF_UP)) + _LEGACY_BLENDED_FIXED_MINOR
    return Money(amount_minor=min(minor, gross.amount_minor), currency=gross.currency)


def blended_or_legacy(gross: Money, *, bps: int, fixed_minor: int, legacy: bool) -> Money:
    """Route a blended merchant to the modern or the legacy calculation.

    ``legacy`` is true only for merchants whose billing profile still carries a
    ``deprecated_at``-less ``fee_schedule`` row in payzeno-billing-legacy. There were
    nine of them at the last count and nobody has scheduled the cutover.
    """
    if legacy:
        return legacy_blended_fee(gross)
    return compute_platform_fee(gross, bps, fixed_minor)


def interchange_plus_components(
    *, markup_bps: int, markup_fixed_minor: int, interchange_minor: int, scheme_fee_minor: int
) -> list[FeeComponent]:
    """Build the component list :func:`apportion_fee` expects for an IC++ merchant.

    Interchange and scheme fees arrive as absolute amounts off the acquirer file, so they
    are expressed as zero-rate components with a fixed part.
    """
    return [
        FeeComponent(name="interchange", bps=0, fixed_minor=interchange_minor),
        FeeComponent(name="scheme_fee", bps=0, fixed_minor=scheme_fee_minor),
        FeeComponent(name=MARKUP_COMPONENT, bps=markup_bps, fixed_minor=markup_fixed_minor),
    ]


def net_of_fee(gross: Money, fee: Money) -> Money:
    """What the merchant is owed after a fee. Never negative."""
    if gross.currency != fee.currency:
        raise ValidationError(
            "fee currency differs from gross",
            details={"gross": gross.currency, "fee": fee.currency},
        )
    if fee.amount_minor > gross.amount_minor:
        return zero(gross.currency)
    return Money(amount_minor=gross.amount_minor - fee.amount_minor, currency=gross.currency)
