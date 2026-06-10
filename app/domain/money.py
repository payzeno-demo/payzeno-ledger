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

_ONE: Final[Decimal] = Decimal(1)


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in minor units plus its currency.

    The Python analogue of ``types.ts``'s ``Money``. Immutable on purpose: a posting line
    that can be mutated after ``PostingRule.validate`` has run is a posting line that can
    unbalance a transaction after it was checked.
    """

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise ValidationError(
                "amount_minor must be an int",
                details={"amount_minor": repr(self.amount_minor)},
            )
        if self.currency not in SUPPORTED_CURRENCIES:
            raise ValidationError(
                "unsupported currency",
                details={"currency": self.currency, "supported": sorted(SUPPORTED_CURRENCIES)},
            )

    def __str__(self) -> str:
        return format_money(self)


@dataclass(frozen=True, slots=True)
class FeeComponent:
    """One addend of a composite fee.

    ``bps`` and ``fixed_minor`` are applied together: ``gross * bps / 10_000 + fixed_minor``.
    ``name`` is the component key that comes back in :class:`FeeBreakdown.components`.
    """

    name: str
    bps: int
    fixed_minor: int = 0


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """The result of apportioning one fee across several components.

    ``sum(components.values()) == total`` always holds — that is the whole point of the
    largest-remainder rule in :func:`app.domain.fees.apportion_fee`. ``remainder_minor``
    records how many minor units the rounding had to move, for the audit trail.
    """

    total: Money
    components: dict[str, Money]
    remainder_minor: int


def exponent_for(currency: str) -> int:
    """Minor-unit exponent for `currency`. JPY is 0 — never assume 2."""
    try:
        return int(CURRENCY_EXPONENT[currency])
    except KeyError as exc:  # pragma: no cover - guarded by Money.__post_init__
        raise ValidationError("unsupported currency", details={"currency": currency}) from exc


def zero(currency: str) -> Money:
    """The additive identity for `currency`."""
    return Money(amount_minor=0, currency=currency)


def is_zero(m: Money) -> bool:
    """True when the amount is exactly zero. Currency is irrelevant to the answer."""
    return m.amount_minor == 0


def assert_same_currency(a: Money, b: Money) -> None:
    """Raise :class:`CurrencyMismatchError` unless both operands share a currency."""
    if a.currency != b.currency:
        raise CurrencyMismatchError(
            "cannot combine amounts in different currencies",
            details={"left": a.currency, "right": b.currency},
        )


def add_money(a: Money, b: Money) -> Money:
    """Sum two amounts of the same currency."""
    assert_same_currency(a, b)
    return Money(amount_minor=a.amount_minor + b.amount_minor, currency=a.currency)


def sub_money(a: Money, b: Money) -> Money:
    """Subtract `b` from `a`. The result may legitimately be negative (variance, drift)."""
    assert_same_currency(a, b)
    return Money(amount_minor=a.amount_minor - b.amount_minor, currency=a.currency)


def negate(m: Money) -> Money:
    """Flip the sign. Used only where a caller reasons in signed deltas (balance cache)."""
    return Money(amount_minor=-m.amount_minor, currency=m.currency)


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


def to_minor(amount: str, currency: str) -> int:
    """Parse a decimal string (``"12.34"``) into minor units for `currency`.

    The settlement parsers call this on every acquirer line, which is why it takes a
    ``str`` and not a float — ``float("0.29") * 100`` is ``28.999999999999996``.
    """
    exponent = exponent_for(currency)
    try:
        value = Decimal(amount.strip())
    except (ArithmeticError, ValueError) as exc:
        raise ValidationError(
            "amount is not a decimal", details={"amount": amount, "currency": currency}
        ) from exc
    scaled = value.scaleb(exponent)
    return int(scaled.quantize(_ONE, rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str) -> str:
    """Render minor units as a plain decimal string with the currency's exponent."""
    exponent = exponent_for(currency)
    if exponent == 0:
        return str(minor)
    quantum = Decimal(1).scaleb(-exponent)
    return str(Decimal(minor).scaleb(-exponent).quantize(quantum))


def format_money(m: Money) -> str:
    """Human-readable form used in ops CLI tables and log context: ``"12.34 USD"``."""
    return f"{from_minor(m.amount_minor, m.currency)} {m.currency}"


def require_positive(m: Money, *, field: str = "amount_minor") -> Money:
    """Guard for ledger entries: direction carries the sign, amounts never do.

    Raises :class:`NegativeAmountError`, which is what invariant 4 of `domain-model.md`
    §7 turns into at ``LedgerPoster.post`` time.
    """
    if m.amount_minor <= 0:
        raise NegativeAmountError(
            "amount must be strictly positive",
            details={"field": field, "amount_minor": m.amount_minor, "currency": m.currency},
        )
    return m


def split_evenly(m: Money, parts: int) -> list[Money]:
    """Split `m` into `parts` amounts that sum exactly to `m`.

    The first ``remainder`` slices carry one extra minor unit. Used by the reserve
    release schedule when a hold is released over several banking days.
    """
    if parts <= 0:
        raise ValidationError("parts must be positive", details={"parts": parts})
    base, remainder = divmod(m.amount_minor, parts)
    return [
        Money(amount_minor=base + (1 if i < remainder else 0), currency=m.currency)
        for i in range(parts)
    ]


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
