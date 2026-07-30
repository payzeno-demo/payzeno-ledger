"""`LedgerAuditService` and `AdjustmentService` — app/services/audit.py.

The most-quoted line in the postmortem is that the nightly trial balance *passed* on the
night of PAY-2041, because a duplicate settlement is internally balanced: two transactions,
six legs, debits equal credits. Every check the ledger had was a balance check, and a
balance check cannot see a duplicate.

`check_duplicate_settlements` is the answer to that (PAY-2054). The first test below is
the one that matters: a duplicated pair passes the trial balance and fails the duplicate
check, in the same fixture, so the distinction is impossible to lose in a refactor.

`AdjustmentService` is here for a different reason — it is the only path to
`AdjustmentPostingRule`, and dual control is the only thing standing between a staff
account and arbitrary entries against merchant money.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.errors import (
    DualControlRequiredError,
    LedgerIntegrityError,
    NotFoundError,
    ValidationError,
)
from app.services.audit import AdjustmentService, LedgerAuditService, TrialBalanceResult
from tests.doubles import CollectingPublisher, FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 3, 0, tzinfo=UTC)


class Totals:
    def __init__(self, debit_minor: int, credit_minor: int) -> None:
        self.debit_minor = debit_minor
        self.credit_minor = credit_minor


class DuplicateRow:
    def __init__(self, key: str, count: int, currency: str, sample: str) -> None:
        self.idempotency_key = key
        self.count = count
        self.currency = currency
        self.sample_transaction_id = sample


class StubEntries:
    def __init__(
        self,
        *,
        totals: Totals | None = None,
        unbalanced: list[str] | None = None,
        recomputed: dict[str, int] | None = None,
    ) -> None:
        self.totals = totals or Totals(1_000_000, 1_000_000)
        self.unbalanced = unbalanced or []
        self.recomputed = recomputed or {"merchant_payable": 0}

    async def trial_balance_by_currency(
        self, session: Any, *, currency: str, as_of: datetime
    ) -> Totals:
        return self.totals

    async def sample_unbalanced_transactions(
        self, session: Any, *, currency: str, as_of: datetime, limit: int
    ) -> list[str]:
        return self.unbalanced[:limit]

    async def sum_by_account_and_purpose(self, session: Any, **kwargs: Any) -> dict[str, int]:
        return self.recomputed


class StubTransactions:
    def __init__(self, duplicates: list[DuplicateRow] | None = None) -> None:
        self.duplicates = duplicates or []
        self.calls: list[dict[str, Any]] = []

    async def list_duplicate_idempotency_keys(
        self, session: Any, *, purpose: str, since: datetime
    ) -> list[DuplicateRow]:
        self.calls.append({"purpose": purpose, "since": since})
        return self.duplicates


class CacheRow:
    def __init__(self, merchant_id: str, currency: str, available_minor: int) -> None:
        self.merchant_id = merchant_id
        self.currency = currency
        self.available_minor = available_minor
        self.livemode = True


class StubBalances:
    def __init__(self, rows: list[CacheRow] | None = None) -> None:
        self.rows = rows or []

    async def list_stale(self, session: Any, *, limit: int) -> list[CacheRow]:
        return self.rows[:limit]


def _audit(
    *,
    entries: StubEntries | None = None,
    transactions: StubTransactions | None = None,
    balances: StubBalances | None = None,
    sessions=None,
):
    publisher = CollectingPublisher()
    service = LedgerAuditService(
        sessions=sessions,
        entries=entries or StubEntries(),
        transactions=transactions or StubTransactions(),
        balances=balances or StubBalances(),
        publisher=publisher,
        clock=FrozenClock(NOW),
    )
    return service, publisher


# --------------------------------------------------------------------------------------
# trial balance — invariant 2
# --------------------------------------------------------------------------------------


async def test_trial_balance_passes_when_debits_equal_credits(sessions_factory) -> None:
    service, publisher = _audit(sessions=sessions_factory)

    result = await service.run_trial_balance(currency="USD")

    assert isinstance(result, TrialBalanceResult)
    assert result.balanced is True
    assert result.delta_minor == 0
    assert publisher.event_types() == []


async def test_trial_balance_raises_and_publishes_when_it_does_not(sessions_factory) -> None:
    service, publisher = _audit(
        entries=StubEntries(
            totals=Totals(1_000_000, 999_000), unbalanced=["txn_bad_1", "txn_bad_2"]
        ),
        sessions=sessions_factory,
    )

    with pytest.raises(LedgerIntegrityError):
        await service.run_trial_balance(currency="USD")

    assert "ledger.imbalance_detected" in publisher.event_types()


async def test_a_duplicated_settlement_still_passes_the_trial_balance(
    sessions_factory,
) -> None:
    """The single most-quoted line in docs/postmortems/2041-duplicate-settlement.md.

    Two settle transactions for the same item is 6 legs instead of 3. Debits still equal
    credits. Every check the ledger had that night was a balance check, and this is why
    all of them passed while $1.42M of merchant payable was overstated.
    """
    entries = StubEntries(totals=Totals(2_000_000, 2_000_000))
    duplicates = [DuplicateRow("settle:sb_QK:ri_X", 2, "USD", "txn_2")]
    service, _ = _audit(
        entries=entries,
        transactions=StubTransactions(duplicates),
        sessions=sessions_factory,
    )

    balanced = await service.run_trial_balance(currency="USD")
    duplicate_keys = await service.check_duplicate_settlements()

    assert balanced.balanced is True
    assert duplicate_keys == 1


# --------------------------------------------------------------------------------------
# duplicate settlements — invariant 1, PAY-2054
# --------------------------------------------------------------------------------------


async def test_duplicate_check_returns_zero_on_a_clean_ledger(sessions_factory) -> None:
    service, publisher = _audit(sessions=sessions_factory)

    assert await service.check_duplicate_settlements() == 0
    assert publisher.event_types() == []


async def test_duplicate_check_only_looks_at_settle_transactions(sessions_factory) -> None:
    """An `auth` and a `capture` for one charge share nothing and are not duplicates."""
    transactions = StubTransactions()
    service, _ = _audit(transactions=transactions, sessions=sessions_factory)

    await service.check_duplicate_settlements()

    assert transactions.calls[0]["purpose"] == "settle"


async def test_duplicate_check_publishes_an_imbalance_event(sessions_factory) -> None:
    """PAY-2055's CloudWatch alarm hangs off this event."""
    duplicates = [
        DuplicateRow("settle:sb_QK:ri_1", 2, "USD", "txn_a"),
        DuplicateRow("settle:sb_QK:ri_2", 2, "USD", "txn_b"),
    ]
    service, publisher = _audit(
        transactions=StubTransactions(duplicates), sessions=sessions_factory
    )

    found = await service.check_duplicate_settlements()

    assert found == 2
    assert "ledger.imbalance_detected" in publisher.event_types()


async def test_duplicate_check_defaults_to_a_bounded_lookback(sessions_factory) -> None:
    """A nightly job that scans 41M rows is a nightly job that gets disabled."""
    transactions = StubTransactions()
    service, _ = _audit(transactions=transactions, sessions=sessions_factory)

    await service.check_duplicate_settlements()

    since = transactions.calls[0]["since"]
    assert since < NOW
    assert NOW - since <= timedelta(days=7)


async def test_duplicate_check_honours_an_explicit_window(sessions_factory) -> None:
    transactions = StubTransactions()
    service, _ = _audit(transactions=transactions, sessions=sessions_factory)
    since = NOW - timedelta(hours=6)

    await service.check_duplicate_settlements(since=since)

    assert transactions.calls[0]["since"] == since


# --------------------------------------------------------------------------------------
# balance cache drift — invariant 3, and the alarm that paged the wrong person
# --------------------------------------------------------------------------------------


async def test_cache_drift_is_reported_per_merchant(sessions_factory) -> None:
    """`LedgerBalanceCacheDrift` is the alarm that woke `apager` at 01:26.

    It fired on a symptom three levels downstream of the cause, which is why the first
    forty minutes of the incident were spent looking at the wrong table.
    """
    balances = StubBalances([CacheRow("mer_drift", "USD", 5_000)])
    entries = StubEntries(recomputed={"merchant_payable": 4_180})
    service, publisher = _audit(
        entries=entries, balances=balances, sessions=sessions_factory
    )

    drifted = await service.check_balance_cache_drift()

    assert drifted == 1
    assert "ledger.imbalance_detected" in publisher.event_types()


async def test_cache_drift_reports_nothing_when_the_cache_agrees(sessions_factory) -> None:
    balances = StubBalances([CacheRow("mer_ok", "USD", 4_180)])
    entries = StubEntries(recomputed={"merchant_payable": 4_180})
    service, publisher = _audit(
        entries=entries, balances=balances, sessions=sessions_factory
    )

    assert await service.check_balance_cache_drift() == 0
    assert publisher.event_types() == []


# --------------------------------------------------------------------------------------
# AdjustmentService — maker/checker
# --------------------------------------------------------------------------------------


class Record:
    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", "adj_1")
        self.merchant_id = kwargs.get("merchant_id", "mer_adj")
        self.currency = kwargs.get("currency", "USD")
        self.livemode = kwargs.get("livemode", True)
        self.reason_code = kwargs.get("reason_code", "goodwill")
        self.requested_by = kwargs.get("requested_by", "staff_a")
        self.approved_by: str | None = None
        self.approved_at: datetime | None = None
        self.status = kwargs.get("status", "pending")
        self.posted_transaction_id: str | None = None
        self.lines = kwargs.get(
            "lines",
            [
                {"account_type": "merchant_payable", "direction": "credit", "amount_minor": 500},
                {"account_type": "platform_expense", "direction": "debit", "amount_minor": 500},
            ],
        )


class StubRequests:
    def __init__(self, rows: dict[str, Record] | None = None) -> None:
        self.rows = rows or {}

    async def add(self, session: Any, obj: Any) -> Any:
        self.rows[obj.id] = obj
        return obj

    async def get(self, session: Any, entity_id: str) -> Any | None:
        return self.rows.get(entity_id)


class StubLedger:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, session: Any, **kwargs: Any) -> Any:
        self.posts.append(kwargs)
        transaction = type("Txn", (), {"id": "txn_adj_1"})()
        return type("PostResult", (), {"transaction": transaction, "created": True})()


def _adjustments(rows: dict[str, Record] | None = None):
    requests = StubRequests(rows)
    ledger = StubLedger()
    service = AdjustmentService(requests=requests, ledger=ledger, clock=FrozenClock(NOW))
    return service, requests, ledger


async def test_requesting_an_adjustment_needs_lines() -> None:
    service, _, _ = _adjustments()

    with pytest.raises(ValidationError):
        await service.request(
            object(),
            merchant_id="mer_adj",
            currency="USD",
            lines=[],
            reason_code="goodwill",
            requested_by="staff_a",
        )


async def test_requesting_an_adjustment_needs_a_reason_code() -> None:
    """An auditor's first question is always "why", and the answer has to be a column."""
    service, _, _ = _adjustments()

    with pytest.raises(ValidationError):
        await service.request(
            object(),
            merchant_id="mer_adj",
            currency="USD",
            lines=[{"account_type": "merchant_payable", "direction": "credit", "amount_minor": 500}],
            reason_code="",
            requested_by="staff_a",
        )


async def test_the_requester_cannot_approve_their_own_adjustment() -> None:
    """Dual control. `chk_adjustment_dual_control` says the same thing in the schema."""
    record = Record(requested_by="staff_a")
    service, _, ledger = _adjustments({record.id: record})

    with pytest.raises(DualControlRequiredError):
        await service.approve(
            object(), record.id, approved_by="staff_a", approver_note="lgtm"
        )

    assert ledger.posts == [], "a rejected approval must not post"


async def test_a_second_approver_posts_the_adjustment() -> None:
    record = Record(requested_by="staff_a")
    service, _, ledger = _adjustments({record.id: record})

    approved = await service.approve(
        object(), record.id, approved_by="staff_b", approver_note="checked with support"
    )

    assert approved.status == "posted"
    assert approved.approved_by == "staff_b"
    assert ledger.posts[0]["purpose"] == "adjustment"
    assert ledger.posts[0]["created_by"] == "admin"


async def test_approving_twice_is_refused() -> None:
    record = Record(requested_by="staff_a", status="posted")
    service, _, ledger = _adjustments({record.id: record})

    with pytest.raises(ValidationError):
        await service.approve(
            object(), record.id, approved_by="staff_b", approver_note="again"
        )


async def test_approving_an_unknown_request_is_a_not_found() -> None:
    service, _, _ = _adjustments()

    with pytest.raises(NotFoundError):
        await service.approve(
            object(), "adj_ghost", approved_by="staff_b", approver_note="?"
        )
