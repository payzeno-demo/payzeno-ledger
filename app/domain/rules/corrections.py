"""Corrections: reversals, approved adjustments, and API-driven refunds.

``ledger_entry`` is append-only — there is no UPDATE and no DELETE on it, ever, enforced
by ``trg_ledger_entry_immutable`` since migration ``0004``. Every correction is therefore
a **new, compensating transaction**, which is why migration ``0020`` quarantines the
incident's 1,847 duplicates by posting reversals rather than by deleting rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from payzeno_contracts.types import LedgerPurpose

from app.domain.postings import Direction, PostingContext, PostingLine, PostingRule, credit, debit
from app.errors import ValidationError


class ReversalPostingRule(PostingRule):
    """Mirror of a prior transaction, leg for leg with the directions flipped.

    A reversal cannot be derived from a :class:`PostingContext` alone — it needs the legs
    it is compensating — so the rule is constructed with them::

        rule = ReversalPostingRule.of(original_lines)
        lines = rule.build(ctx)

    The instance registered in ``POSTING_RULE_BY_PURPOSE`` carries no original and exists
    only so the purpose has an entry; calling ``build`` on it raises, which is the
    behaviour ``AdjustmentService`` and migration ``0019``'s
    ``reverse_duplicate_transactions()`` both rely on to fail loudly rather than post an
    empty correction.
    """

    purpose: ClassVar[LedgerPurpose] = "reversal"

    def __init__(self, original: Sequence[PostingLine] = ()) -> None:
        self._original: tuple[PostingLine, ...] = tuple(original)

    @classmethod
    def of(cls, original: Sequence[PostingLine]) -> ReversalPostingRule:
        """Build a rule that reverses `original`."""
        return cls(original)

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if not self._original:
            raise ValidationError(
                "a reversal needs the original transaction's lines",
                details={"merchant_id": ctx.merchant_id, "currency": ctx.currency},
            )
        flipped: list[PostingLine] = []
        for line in self._original:
            direction: Direction = "credit" if line.direction == "debit" else "debit"
            flipped.append(
                PostingLine(
                    account_type=line.account_type,
                    direction=direction,
                    amount_minor=line.amount_minor,
                )
            )
        return self._built(ctx, flipped)


class AdjustmentPostingRule(PostingRule):
    """Posts an **approved** ``ledger_adjustment_request``.

    Reachable only through a row that passed ``chk_adjustment_dual_control`` — a human
    requested it and a *different* human approved it. ``created_by='admin'`` plus a free
    ``adjustment`` purpose otherwise means a person can post arbitrary entries against
    merchant money with no maker-checker and no reason code, and ``payzeno_ledger`` has
    no ``audit_log`` table of its own to fall back on.

    The legs come off ``ledger_adjustment_request.lines`` (jsonb), decoded by
    ``AdjustmentService.approve`` into ``(account_type, direction, amount_minor)`` triples
    and handed to :meth:`of`.
    """

    purpose: ClassVar[LedgerPurpose] = "adjustment"

    def __init__(self, requested: Sequence[PostingLine] = ()) -> None:
        self._requested: tuple[PostingLine, ...] = tuple(requested)

    @classmethod
    def of(cls, requested: Sequence[PostingLine]) -> AdjustmentPostingRule:
        """Build a rule that posts the approved request's legs."""
        return cls(requested)

    @classmethod
    def from_json_lines(
        cls, raw: Sequence[dict[str, object]]
    ) -> AdjustmentPostingRule:
        """Decode ``ledger_adjustment_request.lines`` into posting lines.

        Rejects anything that is not a complete triple. An adjustment request whose jsonb
        is half-typed must fail at approval time, not at ``LedgerPoster.post`` time —
        the approver is the one who can still fix it.
        """
        lines: list[PostingLine] = []
        for index, entry in enumerate(raw):
            account_type = entry.get("account_type")
            direction = entry.get("direction")
            amount = entry.get("amount_minor")
            if not isinstance(account_type, str) or direction not in ("debit", "credit"):
                raise ValidationError(
                    "adjustment line is missing account_type or direction",
                    details={"index": index, "line": entry},
                )
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                raise ValidationError(
                    "adjustment line amount must be a positive integer",
                    details={"index": index, "amount_minor": amount},
                )
            lines.append(
                PostingLine(
                    account_type=account_type,  # type: ignore[arg-type]
                    direction=direction,
                    amount_minor=amount,
                )
            )
        return cls(lines)

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if not self._requested:
            raise ValidationError(
                "an adjustment needs at least one approved line",
                details={"merchant_id": ctx.merchant_id, "currency": ctx.currency},
            )
        return self._built(ctx, list(self._requested))


class AdjustmentLinePostingRule(PostingRule):
    """A ``line_type='adjustment'`` line in an acquirer settlement file.

    The acquirer is correcting what it owes us. A positive line means they owe more
    (Dr ``processor_clearing``, Cr ``merchant_payable``); a negative one means they owe
    less and the merchant wears it.

    Near-dead: matcher strategies 1-3 never produce it, so in practice it is only ever
    reached after an operator applies ``ManualMatch`` through
    ``POST /internal/v1/ops/items/:itemId/match``.
    """

    purpose: ClassVar[LedgerPurpose] = "adjustment"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        amount_minor = abs(ctx.gross_minor)
        if amount_minor == 0:
            raise ValidationError(
                "adjustment line has no amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        if ctx.gross_minor > 0:
            lines = [
                debit("processor_clearing", amount_minor),
                credit("merchant_payable", amount_minor),
            ]
        else:
            lines = [
                debit("merchant_payable", amount_minor),
                credit("processor_clearing", amount_minor),
            ]
        return self._built(ctx, lines)


class RefundPostingRule(PostingRule):
    """The ``refund`` purpose, driven by ``POST /v1/refunds`` on payzeno-api.

    Dr ``merchant_payable`` (gross) / Cr ``processor_clearing`` when the underlying charge
    is **unsettled** — the refund is netted into the batch — or Cr ``cash`` when it is
    already settled, because the money has left our bank and the merchant's next payout
    carries the reduction.

    A "refund" against an *uncaptured* charge is an authorisation reversal, not a refund:
    it posts ``auth_release`` and creates no ``Refund`` row at all
    (`domain-model.md` §4).
    """

    purpose: ClassVar[LedgerPurpose] = "refund"

    def __init__(self, *, settled: bool = False) -> None:
        self._settled = settled

    @classmethod
    def for_charge(cls, *, settled: bool) -> RefundPostingRule:
        """Pick the credit account for a charge in the given settlement state."""
        return cls(settled=settled)

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        if ctx.gross_minor <= 0:
            raise ValidationError(
                "a refund posting needs a positive amount",
                details={"gross_minor": ctx.gross_minor, "merchant_id": ctx.merchant_id},
            )
        counter_account = "cash" if self._settled else "processor_clearing"
        return self._built(
            ctx,
            [
                debit("merchant_payable", ctx.gross_minor),
                credit(counter_account, ctx.gross_minor),  # type: ignore[arg-type]
            ],
        )
