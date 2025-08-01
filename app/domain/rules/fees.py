"""Fee postings that are not part of a sale.

Two rules, both deliberately narrow:

* :class:`FeePostingRule` — the ``fee`` purpose. **Non-transactional fees only**: monthly
  minimums, account fees, the dispute fee when it is billed separately. It fires roughly
  four times a month. Per-charge processing fees are booked by ``CapturePostingRule`` and
  by nothing else; a ``fee:`` key coexisting with a ``settle:`` key for the same batch and
  charge double-counts both Payzeno's revenue and the merchant's payable deduction
  (`domain-model.md` §0.3, §7).
* :class:`SchemeFeePostingRule` — the ``line_type='scheme_fee'`` line in an acquirer
  settlement file. Scheme assessments are money Payzeno **pays** the network, so they are
  a debit-normal expense, never revenue.
"""

from __future__ import annotations

from typing import ClassVar

from payzeno_contracts.types import LedgerPurpose

from app.domain.postings import PostingContext, PostingLine, PostingRule, credit, debit
from app.errors import ValidationError

#: Fee types the `fee` purpose accepts as its key scope. Anything else is a per-charge
#: processing fee wearing a disguise, and `capture` already booked it.
NON_TRANSACTIONAL_FEE_TYPES: frozenset[str] = frozenset(
    {"monthly_minimum", "account_fee", "dispute_fee", "gateway_fee", "chargeback_fee"}
)


class FeePostingRule(PostingRule):
    """Dr ``merchant_payable`` / Cr ``platform_fee_revenue``.

    Near-dead code by volume — reachable, fired about four times a month by
    ``LedgerAuditService``'s monthly-minimum pass and by the ops adjustment route. It
    stays because the alternative (folding monthly minimums into an adjustment) loses the
    revenue classification that finance reports off ``purpose='fee'``.
    """

    purpose: ClassVar[LedgerPurpose] = "fee"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        amount_minor = ctx.fee_minor or ctx.gross_minor
        if amount_minor <= 0:
            raise ValidationError(
                "a fee posting needs a positive amount",
                details={
                    "fee_minor": ctx.fee_minor,
                    "gross_minor": ctx.gross_minor,
                    "merchant_id": ctx.merchant_id,
                },
            )
        return self._built(
            ctx,
            [
                debit("merchant_payable", amount_minor),
                credit("platform_fee_revenue", amount_minor),
            ],
        )


class SchemeFeePostingRule(PostingRule):
    """An acquirer file's scheme-assessment line.

    Dr ``scheme_fee_expense`` / Cr ``processor_clearing``. The network billed us; the
    acquirer nets it out of what it owes us; it is an expense.

    Scheme fee lines carry no charge, which is why ``reconciliation_item.charge_id`` is
    nullable and why ``SettlementPoster``'s orphan guard has to run *after* the line-type
    dispatch would have been sensible and does not — see `the-incident.md` §3.5.
    """

    purpose: ClassVar[LedgerPurpose] = "fee"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        # Scheme fee lines put the assessment in gross_minor and repeat it in
        # scheme_fee_minor. Prefer the explicit component when the file supplies it.
        amount_minor = ctx.scheme_fee_minor or ctx.gross_minor
        if amount_minor <= 0:
            raise ValidationError(
                "scheme fee line has no amount",
                details={
                    "gross_minor": ctx.gross_minor,
                    "scheme_fee_minor": ctx.scheme_fee_minor,
                },
            )
        return self._built(
            ctx,
            [
                debit("scheme_fee_expense", amount_minor),
                credit("processor_clearing", amount_minor),
            ],
        )


def is_non_transactional(fee_type: str) -> bool:
    """Guard used by ``AdjustmentService`` before it mints a ``fee:`` idempotency key."""
    return fee_type in NON_TRANSACTIONAL_FEE_TYPES
