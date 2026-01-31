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

