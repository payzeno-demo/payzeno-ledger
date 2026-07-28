"""Dispute and chargeback postings.

The disputed amount and the chargeback fee behave differently and therefore sit in
different accounts (`domain-model.md` §5):

===================  =======================  ========================  ================
Leg                  Debit                    Credit                    Reversed on won?
===================  =======================  ========================  ================
disputed amount      ``merchant_payable``     ``chargeback_liability``  yes
chargeback fee       ``merchant_payable``     ``platform_fee_revenue``  no
===================  =======================  ========================  ================

The fee is charged by the network and **retained regardless of who wins**. Booking it
into ``chargeback_liability`` would mean the ``won`` reversal hands it back to the
merchant, which no acquirer does.
"""

from __future__ import annotations

from typing import ClassVar

from payzeno_contracts.types import LedgerPurpose

from app.domain.postings import PostingContext, PostingLine, PostingRule, credit, debit
from app.errors import ValidationError


class DisputePostingRule(PostingRule):
    """A dispute was opened against a captured charge.

    Two legs on the debit side, both against ``merchant_payable``, because the merchant
    loses the disputed amount *and* the fee the moment the issuer raises it. The amount
    comes back on ``won`` (a ``reversal`` of the liability leg only); the fee never does.

    ``ctx.fee_minor`` is looked up from ``dispute_fee_schedule`` by the caller, with
    :data:`app.domain.fees.DEFAULT_DISPUTE_FEE_BY_CURRENCY` as the fallback. It is not a
    constant: 1500 JPY is about $10 and 1500 USD cents is $15.
    """

    purpose: ClassVar[LedgerPurpose] = "dispute"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if ctx.gross_minor <= 0:
            raise ValidationError(
                "a dispute posting needs a positive disputed amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        lines = [
            debit("merchant_payable", ctx.gross_minor),
            credit("chargeback_liability", ctx.gross_minor),
        ]
        if ctx.fee_minor > 0:
            lines.append(debit("merchant_payable", ctx.fee_minor))
            lines.append(credit("platform_fee_revenue", ctx.fee_minor))
        return self._built(ctx, lines)


class ChargebackPostingRule(PostingRule):
    """A ``line_type='chargeback'`` line in an acquirer settlement file.

    By the time the acquirer files it, :class:`DisputePostingRule` has already moved the
    money out of ``merchant_payable`` into ``chargeback_liability``. This line is the
    acquirer actually taking it: the liability is relieved against what they owe us.

    Dr ``chargeback_liability`` / Cr ``processor_clearing``, plus the acquirer's own
    handling fee as an expense when the file carries one.
    """

    purpose: ClassVar[LedgerPurpose] = "dispute"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        amount_minor = abs(ctx.gross_minor)
        if amount_minor == 0:
            raise ValidationError(
                "chargeback line has no amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        lines = [
            debit("chargeback_liability", amount_minor),
            credit("processor_clearing", amount_minor),
        ]
        if ctx.fee_minor > 0:
            lines.append(debit("acquirer_fee_expense", ctx.fee_minor))
            lines.append(credit("processor_clearing", ctx.fee_minor))
        return self._built(ctx, lines)


class ChargebackReversalPostingRule(PostingRule):
    """A ``line_type='chargeback_reversal'`` line — the merchant won, or a representment.

    Dr ``processor_clearing`` / Cr ``chargeback_liability``: the acquirer is giving the
    money back, so what they owe us rises and the liability falls.

    The fee is **not** reversed. If the file carries one it is a second, new handling fee
    for the representment, and it is expensed like any other.
    """

    purpose: ClassVar[LedgerPurpose] = "dispute"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        amount_minor = abs(ctx.gross_minor)
        if amount_minor == 0:
            raise ValidationError(
                "chargeback reversal line has no amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        lines = [
            debit("processor_clearing", amount_minor),
            credit("chargeback_liability", amount_minor),
        ]
        if ctx.fee_minor > 0:
            lines.append(debit("acquirer_fee_expense", ctx.fee_minor))
            lines.append(credit("processor_clearing", ctx.fee_minor))
        return self._built(ctx, lines)
