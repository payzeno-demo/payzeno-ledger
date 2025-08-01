"""The 19 concrete `PostingRule`s — app/domain/rules/**.

One test class per rule, named after the rule. The legs asserted here are the canonical
postings table in domain-model.md §7 and nothing else; if a rule and that table disagree,
the table wins and this file is the thing that says so.

Everything here is pure. No session, no clock, no flags — that is why CI can demand 100%
coverage on app/domain/**.
"""

from __future__ import annotations

import pytest

from app.domain.postings import PostingContext, PostingLine
from app.domain.rules.corrections import (
    AdjustmentLinePostingRule,
    AdjustmentPostingRule,
    RefundPostingRule,
    ReversalPostingRule,
)
from app.domain.rules.disputes import (
    ChargebackPostingRule,
    ChargebackReversalPostingRule,
    DisputePostingRule,
)
from app.domain.rules.fees import FeePostingRule, SchemeFeePostingRule
from app.domain.rules.payouts import (
    PayoutPostingRule,
    PayoutReversalPostingRule,
    ReserveHoldPostingRule,
    ReserveReleasePostingRule,
)
from app.domain.rules.sale import (
    AuthPostingRule,
    AuthReleasePostingRule,
    CapturePostingRule,
    SettlementFundingPostingRule,
    SettlementPostingRule,
    SettlementRefundPostingRule,
)


def make_ctx(**overrides: object) -> PostingContext:
    base: dict[str, object] = {
        "merchant_id": "mer_01HQ8ZR9BB",
        "currency": "USD",
        "livemode": True,
        "gross_minor": 10_000,
        "fee_minor": 290,
        "net_minor": 9_710,
        "interchange_minor": 150,
        "scheme_fee_minor": 13,
        "reserve_bps": 0,
        "platform_fee_bps": 290,
        "platform_fee_fixed_minor": 30,
    }
    base.update(overrides)
    return PostingContext(**base)  # type: ignore[arg-type]


def debits(lines: list[PostingLine]) -> dict[str, int]:
    return {line.account_type: line.amount_minor for line in lines if line.direction == "debit"}


def credits(lines: list[PostingLine]) -> dict[str, int]:
    return {line.account_type: line.amount_minor for line in lines if line.direction == "credit"}


def assert_balanced(lines: list[PostingLine]) -> None:
    assert sum(debits(lines).values()) == sum(credits(lines).values())
    assert len(lines) >= 2


class TestAuthPostingRule:
    def test_is_a_self_reversing_contra_pair(self) -> None:
        lines = AuthPostingRule().build(make_ctx())
        assert_balanced(lines)
        assert debits(lines) == {"merchant_receivable": 10_000}
        assert credits(lines) == {"authorization_hold": 10_000}

    def test_purpose(self) -> None:
        assert AuthPostingRule.purpose == "auth"


class TestAuthReleasePostingRule:
    def test_reverses_the_auth_legs(self) -> None:
        lines = AuthReleasePostingRule().build(make_ctx())
        assert_balanced(lines)
        assert debits(lines) == {"authorization_hold": 10_000}
        assert credits(lines) == {"merchant_receivable": 10_000}


class TestCapturePostingRule:
    def test_creates_the_acquirer_receivable_and_recognises_the_markup(self) -> None:
        lines = CapturePostingRule().build(make_ctx())
        assert_balanced(lines)

        # gross to processor_clearing, plus the inline auth_release debit
        assert debits(lines)["processor_clearing"] == 10_000
        assert debits(lines)["authorization_hold"] == 10_000

        # 2.9% + 30c on $100 = 320
        assert credits(lines)["platform_fee_revenue"] == 320
        assert credits(lines)["merchant_payable"] == 10_000 - 320
        assert credits(lines)["merchant_receivable"] == 10_000

    def test_reserve_leg_only_appears_when_reserve_bps_is_set(self) -> None:
        without = CapturePostingRule().build(make_ctx(reserve_bps=0))
        assert "reserve" not in credits(without)

        with_reserve = CapturePostingRule().build(make_ctx(reserve_bps=1_000))
        assert_balanced(with_reserve)
        assert credits(with_reserve)["reserve"] == 1_000
        # reserve comes out of the merchant's share, not out of Payzeno's markup
        assert credits(with_reserve)["platform_fee_revenue"] == 320
        assert credits(with_reserve)["merchant_payable"] == 10_000 - 320 - 1_000

    def test_acquirer_fee_never_reaches_platform_fee_revenue(self) -> None:
        # fee_minor is what the ACQUIRER kept. Crediting it to revenue overstates gross
        # revenue by the interchange we merely pass through — roughly 80% of the line.
        lines = CapturePostingRule().build(make_ctx(fee_minor=9_000))
        assert credits(lines)["platform_fee_revenue"] == 320


class TestSettlementPostingRule:
    def test_books_the_acquirer_fee_as_expense_against_the_receivable(self) -> None:
        lines = SettlementPostingRule().build(make_ctx())
        assert_balanced(lines)

        assert debits(lines)["interchange_expense"] == 150
        assert debits(lines)["scheme_fee_expense"] == 13
        # acquirer markup is the derived remainder: 290 - 150 - 13
        assert debits(lines)["acquirer_fee_expense"] == 127
        assert credits(lines) == {"processor_clearing": 290}

    def test_expense_legs_sum_to_fee_minor(self) -> None:
        lines = SettlementPostingRule().build(make_ctx(fee_minor=1_000, interchange_minor=700))
        assert sum(debits(lines).values()) == 1_000

    def test_a_zero_fee_line_still_balances(self) -> None:
        lines = SettlementPostingRule().build(
            make_ctx(fee_minor=0, interchange_minor=0, scheme_fee_minor=0)
        )
        assert_balanced(lines)


class TestSettlementFundingPostingRule:
    def test_is_the_only_rule_that_debits_cash(self) -> None:
        lines = SettlementFundingPostingRule().build(make_ctx(gross_minor=1_234_567))
        assert_balanced(lines)
        assert debits(lines) == {"cash": 1_234_567}
        assert credits(lines) == {"processor_clearing": 1_234_567}


class TestSettlementRefundPostingRule:
    def test_returns_the_gross_to_the_acquirer_receivable(self) -> None:
        lines = SettlementRefundPostingRule().build(make_ctx())
        assert_balanced(lines)
        assert debits(lines) == {"merchant_payable": 10_000}
        assert credits(lines) == {"processor_clearing": 10_000}


class TestFeePostingRule:
    def test_is_for_non_transactional_fees_only(self) -> None:
        # Monthly minimum, account fee. NEVER a per-charge processing fee — that is already
        # booked inside `capture`, and booking it twice double-counts revenue.
        lines = FeePostingRule().build(make_ctx(gross_minor=2_500))
        assert_balanced(lines)
        assert debits(lines) == {"merchant_payable": 2_500}
        assert credits(lines) == {"platform_fee_revenue": 2_500}


class TestSchemeFeePostingRule:
    def test_books_a_standalone_scheme_assessment_line(self) -> None:
        lines = SchemeFeePostingRule().build(make_ctx(gross_minor=413, scheme_fee_minor=413))
        assert_balanced(lines)
        assert debits(lines) == {"scheme_fee_expense": 413}
        assert credits(lines) == {"processor_clearing": 413}


class TestDisputePostingRule:
    def test_moves_the_amount_to_liability_and_charges_the_dispute_fee(self) -> None:
        lines = DisputePostingRule().build(make_ctx(gross_minor=4_000, fee_minor=1_500))
        assert_balanced(lines)
        assert debits(lines) == {"merchant_payable": 5_500}
        assert credits(lines)["chargeback_liability"] == 4_000
        assert credits(lines)["platform_fee_revenue"] == 1_500


class TestChargebackPostingRule:
    def test_settles_the_liability_against_the_acquirer(self) -> None:
        lines = ChargebackPostingRule().build(make_ctx(gross_minor=4_000))
        assert_balanced(lines)
        assert debits(lines) == {"chargeback_liability": 4_000}
        assert credits(lines) == {"processor_clearing": 4_000}


class TestChargebackReversalPostingRule:
    def test_gives_the_money_back_when_the_merchant_wins(self) -> None:
        lines = ChargebackReversalPostingRule().build(make_ctx(gross_minor=4_000))
        assert_balanced(lines)
        assert debits(lines) == {"processor_clearing": 4_000}
        assert credits(lines) == {"merchant_payable": 4_000}


class TestPayoutPostingRule:
    def test_debits_the_payable_and_credits_cash(self) -> None:
        lines = PayoutPostingRule().build(make_ctx(gross_minor=418_000))
        assert_balanced(lines)
        assert debits(lines) == {"merchant_payable": 418_000}
        assert credits(lines) == {"cash": 418_000}


class TestPayoutReversalPostingRule:
    def test_is_the_exact_mirror_of_the_payout(self) -> None:
        payout = PayoutPostingRule().build(make_ctx(gross_minor=418_000))
        reversal = PayoutReversalPostingRule().build(make_ctx(gross_minor=418_000))

        assert debits(reversal) == credits(payout)
        assert credits(reversal) == debits(payout)

    def test_without_it_an_ach_failure_destroys_the_merchants_money(self) -> None:
        # invariant 7, domain-model.md §7. `failed` and `returned` are terminal; if nothing
        # gives the money back, it is gone.
        lines = PayoutReversalPostingRule().build(make_ctx(gross_minor=1))
        assert credits(lines) == {"merchant_payable": 1}


class TestReserveHoldPostingRule:
    def test_withholds_from_the_payable(self) -> None:
        lines = ReserveHoldPostingRule().build(make_ctx(gross_minor=1_000))
        assert_balanced(lines)
        assert debits(lines) == {"merchant_payable": 1_000}
        assert credits(lines) == {"reserve": 1_000}


class TestReserveReleasePostingRule:
    def test_returns_the_reserve_to_the_payable(self) -> None:
        lines = ReserveReleasePostingRule().build(make_ctx(gross_minor=1_000))
        assert_balanced(lines)
        assert debits(lines) == {"reserve": 1_000}
        assert credits(lines) == {"merchant_payable": 1_000}


class TestReversalPostingRule:
    def test_mirrors_whatever_it_is_given(self) -> None:
        lines = ReversalPostingRule().build(make_ctx(gross_minor=777))
        assert_balanced(lines)
        assert sum(debits(lines).values()) == 777


class TestAdjustmentPostingRule:
    def test_builds_from_the_approved_request_context(self) -> None:
        lines = AdjustmentPostingRule().build(make_ctx(gross_minor=5_000))
        assert_balanced(lines)

    @pytest.mark.parametrize("amount", [0, -1])
    def test_rejects_a_non_positive_amount(self, amount: int) -> None:
        lines = AdjustmentPostingRule().build(make_ctx(gross_minor=amount))
        with pytest.raises(Exception):  # noqa: B017 — validate() picks the specific class
            AdjustmentPostingRule().validate(lines)


class TestAdjustmentLinePostingRule:
    def test_is_reachable_only_from_a_manual_match(self) -> None:
        # Near-dead code: `ManualMatch` is its only caller and it fires a handful of times a
        # month. It is still the rule an operator's correction goes through, so it is tested.
        lines = AdjustmentLinePostingRule().build(make_ctx(gross_minor=250))
        assert_balanced(lines)


class TestRefundPostingRule:
    def test_unsettled_refund_goes_back_to_the_acquirer(self) -> None:
        lines = RefundPostingRule().build(make_ctx(gross_minor=10_000))
        assert_balanced(lines)
        assert debits(lines) == {"merchant_payable": 10_000}
        assert set(credits(lines)) <= {"processor_clearing", "cash"}
