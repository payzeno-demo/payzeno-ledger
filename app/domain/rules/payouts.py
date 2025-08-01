"""Payout and rolling-reserve postings.

``payout`` moves the merchant's payable balance to their bank; ``payout_reversal``
restores it when ACH gives it back weeks later. Invariant 7 of `domain-model.md` §7:
every payout in ``failed`` or ``returned`` has exactly one ``payout_reversal``
transaction. Without it an ACH failure permanently destroys the merchant's money —
``payout`` already debited ``merchant_payable``, ``failed`` is terminal, and nothing
gives it back.

Reserve is **released, not accumulated**: ``CapturePostingRule`` credits ``reserve``
when ``reserve_bps > 0`` and writes a ``reserve_hold`` row with
``release_on = capture_date + merchant.reserve_hold_days``; ``ReserveReleaseJob`` posts
:class:`ReserveReleasePostingRule` on that date.
"""

from __future__ import annotations

from typing import ClassVar

from payzeno_contracts.types import LedgerPurpose

from app.domain.postings import PostingContext, PostingLine, PostingRule, credit, debit
from app.errors import ValidationError


class PayoutPostingRule(PostingRule):
    """Payout initiated: Dr ``merchant_payable`` / Cr ``cash``.

    Posted inside the same transaction that inserts the ``payout`` row, under the
    merchant/currency advisory lock ``PayoutService.create_payout`` takes before it
    computes the balance (advisory-then-row, ADR 0011).
    """

    purpose: ClassVar[LedgerPurpose] = "payout"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if ctx.gross_minor <= 0:
            raise ValidationError(
                "a payout posting needs a positive amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        return self._built(
            ctx,
            [
                debit("merchant_payable", ctx.gross_minor),
                credit("cash", ctx.gross_minor),
            ],
        )


class PayoutReversalPostingRule(PostingRule):
    """``in_transit → failed`` or ``paid → returned``: Dr ``cash`` / Cr ``merchant_payable``.

    Posted in the **same transaction** as the state change, stamping
    ``payout.reversal_transaction_id``. ``chk_payout_reversal_present`` is the database's
    word on it: a payout row cannot reach ``failed`` or ``returned`` without one.
    """

    purpose: ClassVar[LedgerPurpose] = "payout_reversal"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if ctx.gross_minor <= 0:
            raise ValidationError(
                "a payout reversal needs a positive amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        return self._built(
            ctx,
            [
                debit("cash", ctx.gross_minor),
                credit("merchant_payable", ctx.gross_minor),
            ],
        )


class ReserveHoldPostingRule(PostingRule):
    """A ``line_type='reserve_hold'`` line in an acquirer settlement file.

    Some acquirers withhold their own reserve at the file level rather than letting
    Payzeno do it at capture. Dr ``merchant_payable`` / Cr ``reserve``, and the caller
    writes the matching ``reserve_hold`` row so ``ReserveReleaseJob`` gives it back.

    Note the asymmetry with :class:`~app.domain.rules.sale.CapturePostingRule`, which
    credits ``reserve`` directly out of the capture instead of moving it through
    ``merchant_payable``. Both are correct; they differ because the acquirer-side hold
    lands after the money was already made payable.
    """

    purpose: ClassVar[LedgerPurpose] = "reserve_release"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        amount_minor = abs(ctx.gross_minor)
        if amount_minor == 0:
            raise ValidationError(
                "reserve hold line has no amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        return self._built(
            ctx,
            [
                debit("merchant_payable", amount_minor),
                credit("reserve", amount_minor),
            ],
        )


class ReserveReleasePostingRule(PostingRule):
    """``release_on`` reached: Dr ``reserve`` / Cr ``merchant_payable``.

    Driven by ``ReserveReleaseJob`` off ``pix_reserve_hold_due``. Without this posting a
    merchant on a 10% rolling reserve accumulates money Payzeno can never pay out.
    """

    purpose: ClassVar[LedgerPurpose] = "reserve_release"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        amount_minor = abs(ctx.gross_minor)
        if amount_minor == 0:
            raise ValidationError(
                "reserve release has no amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        return self._built(
            ctx,
            [
                debit("reserve", amount_minor),
                credit("merchant_payable", amount_minor),
            ],
        )
