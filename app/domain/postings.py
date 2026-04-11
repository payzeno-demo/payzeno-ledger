"""Posting rules — the chart-of-accounts logic, one class per canonical posting.

`domain-model.md` §7 is the authority for every leg below. This module owns the
:class:`PostingRule` abstract base, the two frozen dataclasses every rule speaks in, and
:data:`POSTING_RULE_BY_LINE_TYPE` — the **only** place a ``reconciliation_item.line_type``
maps to a rule. ``SettlementPoster`` dispatches through it rather than always building a
sale posting; without that dispatch every refund, chargeback and fee line in an acquirer
file matches no charge and lands in ``orphaned``.

The 19 concretes live in ``app/domain/rules/`` and are re-exported here so callers have
one import site. Layer L0: this package imports ``payzeno_contracts`` and ``app.errors``
and nothing else from the service.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar, Final, Literal

from payzeno_contracts.types import AccountType, LedgerPurpose

from app.domain.money import Money
from app.errors import NegativeAmountError, UnbalancedTransactionError, ValidationError

Direction = Literal["debit", "credit"]


@dataclass(frozen=True, slots=True)
class PostingContext:
    """Everything a rule needs to build its legs, and nothing it could mutate.

    ``fee_minor`` is what the **acquirer** kept — an expense, not revenue.
    ``platform_fee_bps`` / ``platform_fee_fixed_minor`` are Payzeno's own take, sourced
    from ``settlement_charge`` (denormalised at authorisation time) rather than from
    ``merchant_projection``, so a later merchant change cannot retroactively alter an
    in-flight settlement.
    """

    merchant_id: str | None
    currency: str
    livemode: bool
    gross_minor: int
    fee_minor: int
    net_minor: int
    interchange_minor: int
    scheme_fee_minor: int
    reserve_bps: int
    platform_fee_bps: int
    platform_fee_fixed_minor: int

    def money(self, amount_minor: int) -> Money:
        """Attach this context's currency to a bare minor amount."""
        return Money(amount_minor=amount_minor, currency=self.currency)


class PostingLine:
    """One leg of a balanced transaction.

    ``account_type`` is resolved to a concrete ``account.id`` by
    ``AccountResolver.get_or_create`` at posting time — rules never see account ids,
    which is what keeps them pure and testable without a session.
    """

    account_type: AccountType
    direction: Direction
    amount_minor: int

    def signed_minor(self) -> int:
        """Debit-positive signed amount. Used only by :meth:`PostingRule.validate`."""
        return self.amount_minor if self.direction == "debit" else -self.amount_minor


class PostingRule(abc.ABC):
    """Builds the legs of one ledger transaction.

    Subclasses declare the ``purpose`` they post under and implement :meth:`build`.
    There is deliberately no branching outside ``build`` — a rule that inspects the
    session, the clock or a feature flag is not a rule, it is a service.
    """

    purpose: ClassVar[LedgerPurpose]

    @abc.abstractmethod
    def build(self, ctx: PostingContext) -> list[PostingLine]:
        """Return the balanced legs for this posting."""

    def validate(self, lines: list[PostingLine]) -> None:
        """Enforce invariants 1, 2 and 4 of `domain-model.md` §7.

        1. ``sum(debits) == sum(credits)`` — else :class:`UnbalancedTransactionError`.
        2. at least two entries — else :class:`UnbalancedTransactionError`.
        4. every ``amount_minor > 0`` — else :class:`NegativeAmountError`.

        Currency (3) and livemode (5) are checked by ``LedgerPoster.post``, which is the
        only thing that knows the transaction header the lines will hang off.
        """
        if len(lines) < 2:
            raise UnbalancedTransactionError(
                "a transaction needs at least two entries",
                details={"purpose": self.purpose, "line_count": len(lines)},
            )
        for index, line in enumerate(lines):
            if line.amount_minor <= 0:
                raise NegativeAmountError(
                    "entry amounts must be strictly positive; direction carries the sign",
                    details={
                        "purpose": self.purpose,
                        "sequence": index,
                        "amount_minor": line.amount_minor,
                        "account_type": line.account_type,
                    },
                )
        delta = sum(line.signed_minor() for line in lines)
        if delta != 0:
            raise UnbalancedTransactionError(
                "debits do not equal credits",
                details={
                    "purpose": self.purpose,
                    "debit_minus_credit_minor": delta,
                    "line_count": len(lines),
                },
            )

    def _built(self, ctx: PostingContext, lines: list[PostingLine]) -> list[PostingLine]:
        """Drop zero-amount legs, then validate. Every concrete ends with this call.

        Zero legs are normal: a merchant with ``reserve_bps == 0`` has no reserve leg and
        a fee-free line has no expense leg. Emitting them would violate invariant 4.
        """
        kept = [line for line in lines if line.amount_minor != 0]
        self.validate(kept)
        if ctx.currency and not ctx.currency.strip():
            raise ValidationError("posting context has no currency", details={})
        return kept


def debit(account_type: AccountType, amount_minor: int) -> PostingLine:
    """Shorthand used by every rule body."""
    return PostingLine(account_type=account_type, direction="debit", amount_minor=amount_minor)


def credit(account_type: AccountType, amount_minor: int) -> PostingLine:
    """Shorthand used by every rule body."""
    return PostingLine(account_type=account_type, direction="credit", amount_minor=amount_minor)


from app.domain.rules.corrections import (  # noqa: E402  (cycle-safe: see module docstring)
    AdjustmentLinePostingRule,
    AdjustmentPostingRule,
    RefundPostingRule,
    ReversalPostingRule,
)
from app.domain.rules.disputes import (  # noqa: E402
    ChargebackPostingRule,
    ChargebackReversalPostingRule,
    DisputePostingRule,
)
from app.domain.rules.fees import FeePostingRule, SchemeFeePostingRule  # noqa: E402
from app.domain.rules.payouts import (  # noqa: E402
    PayoutPostingRule,
    PayoutReversalPostingRule,
    ReserveHoldPostingRule,
    ReserveReleasePostingRule,
)
from app.domain.rules.sale import (  # noqa: E402
    AuthPostingRule,
    AuthReleasePostingRule,
    CapturePostingRule,
    SettlementFundingPostingRule,
    SettlementPostingRule,
    SettlementRefundPostingRule,
)

#: Rule instances are stateless, so one shared instance per rule is correct and cheap.
POSTING_RULE_BY_PURPOSE: Final[dict[str, PostingRule]] = {
    "auth": AuthPostingRule(),
    "auth_release": AuthReleasePostingRule(),
    "capture": CapturePostingRule(),
    "settle": SettlementPostingRule(),
    "settlement_funding": SettlementFundingPostingRule(),
    "fee": FeePostingRule(),
    "refund": RefundPostingRule(),
    "dispute": DisputePostingRule(),
    "reserve_release": ReserveReleasePostingRule(),
    "payout": PayoutPostingRule(),
    "payout_reversal": PayoutReversalPostingRule(),
    "reversal": ReversalPostingRule(),
    "adjustment": AdjustmentPostingRule(),
}

#: The ONLY place `reconciliation_item.line_type` maps to a rule. `domain-model.md` §8.
POSTING_RULE_BY_LINE_TYPE: Final[dict[str, PostingRule]] = {
    "sale": POSTING_RULE_BY_PURPOSE["settle"],
    "refund": SettlementRefundPostingRule(),
    "chargeback": ChargebackPostingRule(),
    "chargeback_reversal": ChargebackReversalPostingRule(),
    "scheme_fee": SchemeFeePostingRule(),
    "adjustment": AdjustmentLinePostingRule(),
    "reserve_hold": ReserveHoldPostingRule(),
    "reserve_release": POSTING_RULE_BY_PURPOSE["reserve_release"],
}

__all__ = [
    "POSTING_RULE_BY_LINE_TYPE",
    "POSTING_RULE_BY_PURPOSE",
    "AdjustmentLinePostingRule",
    "AdjustmentPostingRule",
    "AuthPostingRule",
    "AuthReleasePostingRule",
    "CapturePostingRule",
    "ChargebackPostingRule",
    "ChargebackReversalPostingRule",
    "DisputePostingRule",
    "FeePostingRule",
    "PayoutPostingRule",
    "PayoutReversalPostingRule",
    "PostingContext",
    "PostingLine",
    "PostingRule",
    "RefundPostingRule",
    "ReserveHoldPostingRule",
    "ReserveReleasePostingRule",
    "ReversalPostingRule",
    "SchemeFeePostingRule",
    "SettlementFundingPostingRule",
    "SettlementPostingRule",
    "SettlementRefundPostingRule",
    "credit",
    "debit",
]
