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


class FxPayoutSourcePostingRule(PostingRule):
    """A converted payout, the balance side: Dr ``merchant_payable`` / Cr ``fx_clearing``.

    Posted in the **source** currency for ``gross_minor`` (the amount drawn from the
    balance). Its sibling :class:`FxPayoutTargetPostingRule` posts the bank side in the
    target currency. Two transactions because invariant 3 is one currency per transaction;
    each balances on its own, and ``fx_clearing`` carries the position between them.
    """

    purpose: ClassVar[LedgerPurpose] = "payout"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if ctx.gross_minor <= 0:
            raise ValidationError(
                "a converted payout needs a positive source amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        return self._built(
            ctx,
            [
                debit("merchant_payable", ctx.gross_minor),
                credit("fx_clearing", ctx.gross_minor),
            ],
        )


class FxPayoutTargetPostingRule(PostingRule):
    """A converted payout, the bank side, in the **target** currency.

    Dr ``fx_clearing`` (``gross_minor``: the source at the mid-rate)
    Cr ``cash`` (``net_minor``: what lands in the bank) + ``fx_revenue`` (``fee_minor``:
    the markup). ``gross == net + fee`` is checked rather than assumed — the three come
    from :func:`app.domain.fx.quote_payout_conversion` and a caller that assembled them by
    hand is the one place they could disagree.
    """

    purpose: ClassVar[LedgerPurpose] = "payout"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if ctx.net_minor <= 0 or ctx.fee_minor < 0:
            raise ValidationError(
                "a converted payout needs a positive payout amount and a non-negative spread",
                details={
                    "net_minor": ctx.net_minor,
                    "fee_minor": ctx.fee_minor,
                    "merchant_id": ctx.merchant_id,
                },
            )
        if ctx.gross_minor != ctx.net_minor + ctx.fee_minor:
            raise ValidationError(
                "mid amount must equal payout plus spread",
                details={
                    "gross_minor": ctx.gross_minor,
                    "net_minor": ctx.net_minor,
                    "fee_minor": ctx.fee_minor,
                },
            )
        return self._built(
            ctx,
            [
                debit("fx_clearing", ctx.gross_minor),
                credit("cash", ctx.net_minor),
                credit("fx_revenue", ctx.fee_minor),
            ],
        )


def mirrored(lines: list[PostingLine]) -> list[PostingLine]:
    """The same legs with every direction flipped — a payout reversal's shape.

    A failed or returned converted payout gives back exactly what the conversion took, at
    the rate it was taken: the merchant's source balance is restored in full and the
    spread is un-booked. No second conversion, so no FX gain or loss on a return.
    """
    return [
        PostingLine(
            account_type=line.account_type,
            direction="credit" if line.direction == "debit" else "debit",
            amount_minor=line.amount_minor,
        )
        for line in lines
    ]


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
