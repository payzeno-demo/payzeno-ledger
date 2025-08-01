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

