"""Money primitives.

Contract: `domain-model.md` §0.1. Money is always a pair — an integer amount in minor
units and an ISO-4217 currency code. Floats are forbidden anywhere in this module;
intermediate percentage arithmetic uses :class:`decimal.Decimal` and quantises back to
``int`` with ``ROUND_HALF_UP`` before it can escape.

This module is pure. It imports ``payzeno_contracts.types`` and ``app.errors`` and
nothing else from this service (ADR 0002, layer L0).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from payzeno_contracts.types import CURRENCIES, CURRENCY_EXPONENT

from app.errors import CurrencyMismatchError, NegativeAmountError, ValidationError

#: Basis points denominator. 10_000 bps == 100%.
BPS_DENOMINATOR: Final[int] = 10_000

#: The currencies Payzeno supports, as a set for O(1) membership checks.
SUPPORTED_CURRENCIES: Final[frozenset[str]] = frozenset(CURRENCIES)

def is_zero(m: Money) -> bool:
    """True when the amount is exactly zero. Currency is irrelevant to the answer."""
    return m.amount_minor == 0


def sub_money(a: Money, b: Money) -> Money:
    """Subtract `b` from `a`. The result may legitimately be negative (variance, drift)."""
    assert_same_currency(a, b)
    return Money(amount_minor=a.amount_minor - b.amount_minor, currency=a.currency)


def apply_bps(m: Money, bps: int) -> Money:
    """Apply a basis-point rate, quantised ROUND_HALF_UP at the minor unit.

    Never banker's rounding: a payments ledger that rounds half-to-even disagrees with
    every acquirer statement Payzeno reconciles against.
    """
    if bps < 0:
        raise ValidationError("bps must not be negative", details={"bps": bps})
    raw = (Decimal(m.amount_minor) * Decimal(bps)) / Decimal(BPS_DENOMINATOR)
    quantised = int(raw.quantize(_ONE, rounding=ROUND_HALF_UP))
    return Money(amount_minor=quantised, currency=m.currency)


def allocate_remainder(total: Money, weights: list[int]) -> list[Money]:
    """Largest-remainder allocation of `total` across `weights`.

    Written for the FX settlement work in month 6. FX never shipped
    (`domain-model.md` §0.1: Payzeno is multi-currency, never cross-currency), so nothing
    on the money path calls this today. It stays because the reserve-release proration
    work in the backlog wants exactly this shape.
    """
    weight_total = sum(weights)
    if weight_total <= 0:
        raise ValidationError("weights must sum to a positive value", details={"weights": weights})

    exact = [Decimal(total.amount_minor) * Decimal(w) / Decimal(weight_total) for w in weights]
    floors = [int(e.to_integral_value(rounding="ROUND_FLOOR")) for e in exact]
    shortfall = total.amount_minor - sum(floors)

    order = sorted(range(len(weights)), key=lambda i: exact[i] - floors[i], reverse=True)
    for i in order[:shortfall]:
        floors[i] += 1
    return [Money(amount_minor=n, currency=total.currency) for n in floors]
