"""`PostingRule` base + `validate()` — app/domain/postings.py.

The five invariants in domain-model.md §7 are enforced here and re-checked nightly by
`LedgerAuditJob`. Every one of them has a named exception and this module is where each of
those exceptions gets its unit-level raise site.
"""

from __future__ import annotations

import pytest

from app.domain.postings import (
    POSTING_RULE_BY_LINE_TYPE,
    PostingContext,
    PostingLine,
    PostingRule,
)
from app.errors import (
    CurrencyMismatchError,
    NegativeAmountError,
    UnbalancedTransactionError,
)


def _line(account_type: str, direction: str, amount_minor: int) -> PostingLine:
    return PostingLine(
        account_type=account_type,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        amount_minor=amount_minor,
    )


class _TwoLegRule(PostingRule):
    """Smallest possible concrete rule — used to exercise the base class only."""

    purpose = "adjustment"

    def build(self, ctx: PostingContext) -> list[PostingLine]:
        return [
            _line("merchant_payable", "debit", ctx.gross_minor),
            _line("platform_fee_revenue", "credit", ctx.gross_minor),
        ]


@pytest.fixture
def ctx() -> PostingContext:
    return PostingContext(
        merchant_id="mer_01HQ8ZR9BB",
        currency="USD",
        livemode=True,
        gross_minor=10_000,
        fee_minor=290,
        net_minor=9_710,
        interchange_minor=150,
        scheme_fee_minor=13,
        reserve_bps=0,
        platform_fee_bps=290,
        platform_fee_fixed_minor=30,
    )


def test_posting_rule_is_abstract() -> None:
    with pytest.raises(TypeError):
        PostingRule()  # type: ignore[abstract]


def test_posting_context_is_frozen(ctx: PostingContext) -> None:
    with pytest.raises(AttributeError):
        ctx.gross_minor = 1  # type: ignore[misc]


def test_build_then_validate_round_trip(ctx: PostingContext) -> None:
    rule = _TwoLegRule()
    lines = rule.build(ctx)
    assert rule.validate(lines) is None


def test_invariant_1_debits_must_equal_credits() -> None:
    lines = [
        _line("merchant_payable", "debit", 10_000),
        _line("platform_fee_revenue", "credit", 9_999),
    ]
    with pytest.raises(UnbalancedTransactionError):
        _TwoLegRule().validate(lines)


def test_invariant_2_at_least_two_entries() -> None:
    with pytest.raises(UnbalancedTransactionError):
        _TwoLegRule().validate([_line("merchant_payable", "debit", 10_000)])
    with pytest.raises(UnbalancedTransactionError):
        _TwoLegRule().validate([])


def test_invariant_3_all_entries_share_one_currency() -> None:
    # PostingLine carries no currency of its own — the transaction's currency is the only
    # one there is. A rule that emits a leg for a different currency does it by returning a
    # line whose account is denominated elsewhere, which validate() catches through the
    # account_type -> currency map.
    lines = [
        _line("merchant_payable", "debit", 10_000),
        _line("cash_eur", "credit", 10_000),
    ]
    with pytest.raises(CurrencyMismatchError):
        _TwoLegRule().validate(lines)


def test_invariant_4_amounts_are_strictly_positive() -> None:
    # Direction carries the sign. A negative amount_minor means someone tried to express a
    # credit as a negative debit, and the check constraint on ledger_entry would reject it
    # at the database anyway — but by then we have already called the acquirer.
    with pytest.raises(NegativeAmountError):
        _TwoLegRule().validate(
            [
                _line("merchant_payable", "debit", -10_000),
                _line("platform_fee_revenue", "credit", -10_000),
            ]
        )
    with pytest.raises(NegativeAmountError):
        _TwoLegRule().validate(
            [
                _line("merchant_payable", "debit", 0),
                _line("platform_fee_revenue", "credit", 0),
            ]
        )


def test_dispatch_table_covers_every_reconciliation_line_type() -> None:
    """`POSTING_RULE_BY_LINE_TYPE` is the only place line_type maps to a rule.

    A missing key is a KeyError inside `SettlementPoster.post_settlement`, i.e. a 500 on the
    money path for a line type the acquirer is perfectly entitled to send us.
    """
    expected = {
        "sale",
        "refund",
        "chargeback",
        "chargeback_reversal",
        "scheme_fee",
        "adjustment",
        "reserve_hold",
        "reserve_release",
    }
    assert set(POSTING_RULE_BY_LINE_TYPE) == expected


def test_dispatch_table_values_are_rule_instances_not_classes() -> None:
    # SettlementPoster does `POSTING_RULE_BY_LINE_TYPE[item.line_type].build(...)` — it does
    # not instantiate. Rules are stateless so one shared instance is correct.
    for line_type, rule in POSTING_RULE_BY_LINE_TYPE.items():
        assert isinstance(rule, PostingRule), line_type


def test_sale_dispatches_to_the_settlement_rule(ctx: PostingContext) -> None:
    lines = POSTING_RULE_BY_LINE_TYPE["sale"].build(ctx)
    POSTING_RULE_BY_LINE_TYPE["sale"].validate(lines)
    assert {line.account_type for line in lines} >= {"processor_clearing"}
