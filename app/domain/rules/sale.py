"""The sale path: auth, auth release, capture, settle, settlement funding, file refunds.

For one settled sale charge, in order, these purposes post and no others:
``auth`` → ``capture`` (which carries ``auth_release`` inline) → ``settle`` → (batch)
``settlement_funding``. There is **no** ``fee`` transaction for that charge —
`domain-model.md` §7.
"""

from __future__ import annotations

from typing import ClassVar

from payzeno_contracts.types import LedgerPurpose

from app.domain.fees import acquirer_markup_minor, compute_platform_fee
from app.domain.money import apply_bps
from app.domain.postings import PostingContext, PostingLine, PostingRule, credit, debit
from app.errors import ValidationError


class AuthPostingRule(PostingRule):
    """Authorisation approved: a self-reversing contra pair, no P&L.

    Dr ``merchant_receivable`` (gross) / Cr ``authorization_hold`` (gross).
    """

    purpose: ClassVar[LedgerPurpose] = "auth"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        return self._built(
            ctx,
            [
                debit("merchant_receivable", ctx.gross_minor),
                credit("authorization_hold", ctx.gross_minor),
            ],
        )


class AuthReleasePostingRule(PostingRule):
    """Release of the hold on capture, void or expiry. Always released exactly once.

    Dr ``authorization_hold`` (gross) / Cr ``merchant_receivable`` (gross). Posted as its
    own transaction by ``ChargeExpiryJob``-driven voids and expiries, and inline by
    :class:`CapturePostingRule` on the capture path.
    """

    purpose: ClassVar[LedgerPurpose] = "auth_release"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        return self._built(
            ctx,
            [
                debit("authorization_hold", ctx.gross_minor),
                credit("merchant_receivable", ctx.gross_minor),
            ],
        )


class CapturePostingRule(PostingRule):
    """Capture — where the acquirer receivable is created and Payzeno's take recognised.

    Dr ``processor_clearing`` (gross)
    Cr ``merchant_payable`` (net) + ``platform_fee_revenue`` (markup) + ``reserve``
    (when ``reserve_bps > 0``), **plus** the ``auth_release`` legs in the same
    transaction so the hold and the capture can never drift apart.

    ``net = gross - markup - reserve``. The markup is
    :func:`app.domain.fees.compute_platform_fee` over the merchant's own bps and fixed
    amount, which is why a merchant repricing mid-flight cannot change an already
    authorised charge: those two numbers are denormalised onto ``settlement_charge``.

    When ``reserve_bps > 0`` the caller also writes a ``reserve_hold`` row with
    ``release_on = capture_date + merchant.reserve_hold_days`` — reserve is *released*,
    never merely accumulated.
    """

    purpose: ClassVar[LedgerPurpose] = "capture"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        gross = ctx.money(ctx.gross_minor)
        markup = compute_platform_fee(gross, ctx.platform_fee_bps, ctx.platform_fee_fixed_minor)
        reserve = apply_bps(gross, ctx.reserve_bps)

        net_minor = ctx.gross_minor - markup.amount_minor - reserve.amount_minor
        if net_minor < 0:
            raise ValidationError(
                "platform fee plus reserve exceeds the captured amount",
                details={
                    "gross_minor": ctx.gross_minor,
                    "markup_minor": markup.amount_minor,
                    "reserve_minor": reserve.amount_minor,
                    "merchant_id": ctx.merchant_id,
                },
            )

        return self._built(
            ctx,
            [
                debit("processor_clearing", ctx.gross_minor),
                credit("merchant_payable", net_minor),
                credit("platform_fee_revenue", markup.amount_minor),
                credit("reserve", reserve.amount_minor),
                # auth_release, inline: the hold this capture consumes.
                debit("authorization_hold", ctx.gross_minor),
                credit("merchant_receivable", ctx.gross_minor),
            ],
        )


class SettlementPostingRule(PostingRule):
    """One acquirer sale line, when the settlement file arrives.

    Dr ``interchange_expense`` + ``scheme_fee_expense`` + ``acquirer_fee_expense``
    (summing to ``fee_minor``) / Cr ``processor_clearing`` (``fee_minor``).

    The acquirer **nets its fee out of what it owes us**, so the fee reduces the
    receivable and lands in expense — it is not revenue. Payzeno's own markup was already
    recognised by :class:`CapturePostingRule`; booking it again here double-counts both
    revenue and the merchant's payable deduction.
    """

    purpose: ClassVar[LedgerPurpose] = "settle"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if ctx.fee_minor <= 0:
            # A sale line the acquirer charged nothing for has no legs to post: the
            # receivable was created in full at capture and nothing is being relieved.
            # Those lines exist (intra-regional promo rates, correction re-files) and the
            # caller marks the item settled against the batch funding posting instead.
            raise ValidationError(
                "settle posting requires a non-zero acquirer fee",
                details={
                    "fee_minor": ctx.fee_minor,
                    "gross_minor": ctx.gross_minor,
                    "merchant_id": ctx.merchant_id,
                },
            )

        # The three expense legs must sum to fee_minor exactly, whatever the file says.
        # Downgrade correction lines occasionally carry interchange > total fee; clamping
        # each component in turn keeps the transaction balanced and leaves the variance
        # check in SettlementPoster to reject a genuinely wrong file.
        interchange_minor = min(max(ctx.interchange_minor, 0), ctx.fee_minor)
        scheme_minor = min(max(ctx.scheme_fee_minor, 0), ctx.fee_minor - interchange_minor)
        markup_minor = acquirer_markup_minor(ctx.fee_minor, interchange_minor, scheme_minor)

        return self._built(
            ctx,
            [
                debit("interchange_expense", interchange_minor),
                debit("scheme_fee_expense", scheme_minor),
                debit("acquirer_fee_expense", markup_minor),
                credit("processor_clearing", ctx.fee_minor),
            ],
        )


class SettlementFundingPostingRule(PostingRule):
    """The batch was actually paid: cash follows the bank, not the file.

    Dr ``cash`` (funded amount) / Cr ``processor_clearing`` (funded amount). Posted
    **once per batch**, only when a ``funding_event`` matches within
    ``FUNDING_MATCH_TOLERANCE_BPS``. This is the only posting that debits ``cash``.

    An acquirer that files a batch and then short-pays therefore leaves
    ``processor_clearing`` outstanding rather than leaving the ledger claiming cash
    Payzeno does not have.
    """

    purpose: ClassVar[LedgerPurpose] = "settlement_funding"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        return self._built(
            ctx,
            [
                debit("cash", ctx.gross_minor),
                credit("processor_clearing", ctx.gross_minor),
            ],
        )


class SettlementRefundPostingRule(PostingRule):
    """A ``line_type='refund'`` line inside an acquirer settlement file.

    The refund is netted into the batch: the merchant's payable is debited and what the
    acquirer owes us falls by the same amount. The acquirer keeps its refund handling
    fee, which is expensed exactly like a sale fee.
    """

    purpose: ClassVar[LedgerPurpose] = "refund"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        lines = [
            debit("merchant_payable", ctx.gross_minor),
            credit("processor_clearing", ctx.gross_minor),
        ]
        if ctx.fee_minor > 0:
            lines.append(debit("acquirer_fee_expense", ctx.fee_minor))
            lines.append(credit("processor_clearing", ctx.fee_minor))
        return self._built(ctx, lines)
