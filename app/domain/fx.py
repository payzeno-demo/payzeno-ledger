"""Currency conversion arithmetic — RFC platform-v2 §7.

Payzeno was multi-currency and never cross-currency (`domain-model.md` §0.1) until payouts
learned to convert: a merchant's EUR balance can now be paid into a USD bank account. This
module is the arithmetic for that and nothing else. It is pure — rates come in as
:class:`~decimal.Decimal`, money as :class:`~app.domain.money.Money`, and nothing here
fetches, caches or remembers a rate.

Three rules, all of them about not inventing or losing a minor unit:

* **Decimal, never float.** A provider rate arrives as a decimal string and stays a
  ``Decimal`` until it is quantised to :data:`RATE_PLACES` places.
* **Amounts are computed from the recorded rate.** ``target = source x rate``, rounded once
  to the target currency's exponent (JPY 0, the rest 2), ``ROUND_HALF_UP`` like every other
  rounding in the ledger. The rate stored on the payout reproduces the payout amount.
* **The spread is a difference of two roundings, never its own.** ``spread = round(source
  x mid) - round(source x rate)``. The FX legs then balance by construction: the target
  currency's ``fx_clearing`` is debited the mid amount, ``cash`` is credited the payout
  and ``fx_revenue`` the spread.

Layer L0: imports ``payzeno_contracts`` (through ``app.domain.money``) and ``app.errors``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

from app.domain.money import BPS_DENOMINATOR, SUPPORTED_CURRENCIES, Money, exponent_for
from app.errors import ValidationError

__all__ = [
    "RATE_PLACES",
    "FxConversion",
    "convert_minor",
    "cross_rate",
    "format_rate",
    "marked_down_rate",
    "parse_rate",
    "quote_payout_conversion",
]

#: Decimal places a rate is quantised to before it is used or recorded. Ten is more than
#: any provider quotes (four or six) and few enough that a cross rate through USD is
#: still exact to well under a minor unit on the largest payout a rail will carry.
RATE_PLACES: Final[int] = 10
_RATE_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-RATE_PLACES)
_ONE: Final[Decimal] = Decimal(1)


@dataclass(frozen=True, slots=True)
class FxConversion:
    """One conversion of a balance amount into a bank currency, fully determined.

    ``target`` is what lands in the bank. ``mid_target_minor`` is what the source would
    have bought at the provider mid-rate; the difference is ``spread_minor``, Payzeno's FX
    revenue, in the target currency.
    """

    source: Money
    target: Money
    mid_rate: Decimal
    rate: Decimal
    markup_bps: int
    mid_target_minor: int

    @property
    def spread_minor(self) -> int:
        return self.mid_target_minor - self.target.amount_minor


def parse_rate(raw: object, *, currency: str) -> Decimal:
    """A provider rate as a positive ``Decimal``. Strings only — a float is refused.

    ``float`` is refused rather than converted because ``Decimal(0.9231)`` is
    ``0.92310000000000003...``; a provider that sends JSON numbers has to be parsed with
    ``parse_float=Decimal`` upstream so the literal survives, and then arrives here as a
    ``Decimal``.
    """
    if isinstance(raw, bool | float):
        raise ValidationError(
            "fx rates must be decimal strings", details={"currency": currency, "rate": repr(raw)}
        )
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(
            "fx rate is not a decimal", details={"currency": currency, "rate": repr(raw)}
        ) from exc
    if not value.is_finite() or value <= 0:
        raise ValidationError(
            "fx rate must be positive", details={"currency": currency, "rate": str(value)}
        )
    return value


def cross_rate(rates: Mapping[str, Decimal], from_currency: str, to_currency: str) -> Decimal:
    """Units of ``to_currency`` per one ``from_currency``, through a shared base.

    ``rates`` maps each currency to its units per one base unit (the provider quotes
    ``base=USD``, so ``rates["USD"] == 1``). The cross is ``rates[to] / rates[from]``,
    quantised to :data:`RATE_PLACES`.
    """
    for currency in (from_currency, to_currency):
        if currency not in SUPPORTED_CURRENCIES:
            raise ValidationError("unsupported currency", details={"currency": currency})
        if currency not in rates:
            raise ValidationError(
                "no fx rate for currency",
                details={"currency": currency, "quoted": sorted(rates)},
            )
    if from_currency == to_currency:
        return _ONE
    return (rates[to_currency] / rates[from_currency]).quantize(
        _RATE_QUANTUM, rounding=ROUND_HALF_UP
    )


def marked_down_rate(mid_rate: Decimal, markup_bps: int) -> Decimal:
    """The rate a payout converts at: the mid-rate less Payzeno's markup.

    A payout *sells* the merchant's balance, so the markup comes off the rate the merchant
    receives — ``mid x (1 - bps/10_000)``. (Checkout quotes go the other way, ``mid x (1 +
    bps)``, because there the shopper is buying.)
    """
    if markup_bps < 0 or markup_bps >= BPS_DENOMINATOR:
        raise ValidationError("markup_bps out of range", details={"markup_bps": markup_bps})
    factor = Decimal(BPS_DENOMINATOR - markup_bps) / Decimal(BPS_DENOMINATOR)
    return (mid_rate * factor).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)


def convert_minor(amount_minor: int, from_currency: str, to_currency: str, rate: Decimal) -> int:
    """``amount_minor`` of ``from_currency`` in ``to_currency`` minor units at ``rate``.

    The exponent difference is applied before rounding, so USD 10.00 → JPY at 151.20 is
    ``1000 x 151.20 x 10^(0-2) = 1512`` yen and JPY 1512 → USD at 1/151.20 is 1000 cents.
    """
    shift = exponent_for(to_currency) - exponent_for(from_currency)
    exact = Decimal(amount_minor) * rate
    return int(exact.scaleb(shift).quantize(_ONE, rounding=ROUND_HALF_UP))


def quote_payout_conversion(
    source: Money, to_currency: str, *, mid_rate: Decimal, markup_bps: int
) -> FxConversion:
    """Everything a converted payout records and posts, from one source amount.

    Raises :class:`ValidationError` when the source is not positive, the currencies are
    the same (there is nothing to convert), or the amount is too small to survive the
    conversion as at least one target minor unit.
    """
    if source.amount_minor <= 0:
        raise ValidationError(
            "a conversion needs a positive amount",
            details={"amount_minor": source.amount_minor, "currency": source.currency},
        )
    if source.currency == to_currency:
        raise ValidationError(
            "source and target currency are the same", details={"currency": to_currency}
        )
    rate = marked_down_rate(mid_rate, markup_bps)
    target_minor = convert_minor(source.amount_minor, source.currency, to_currency, rate)
    mid_minor = convert_minor(source.amount_minor, source.currency, to_currency, mid_rate)
    if target_minor <= 0:
        raise ValidationError(
            "amount is too small to convert",
            details={
                "amount_minor": source.amount_minor,
                "currency": source.currency,
                "to_currency": to_currency,
                "rate": format_rate(rate),
            },
        )
    return FxConversion(
        source=source,
        target=Money(amount_minor=target_minor, currency=to_currency),
        mid_rate=mid_rate,
        rate=rate,
        markup_bps=markup_bps,
        mid_target_minor=mid_minor,
    )


def format_rate(rate: Decimal) -> str:
    """The wire form of a rate: plain decimal notation, no exponent, no trailing zeros."""
    return format(rate.normalize(), "f")
