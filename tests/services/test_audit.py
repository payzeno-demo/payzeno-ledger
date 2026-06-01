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
    async def trial_balance_by_currency(
        self, session: Any, *, currency: str, as_of: datetime
    ) -> Totals:
        return self.totals

    async def list_stale(self, session: Any, *, limit: int) -> list[CacheRow]:
        return self.rows[:limit]


def _audit(
    transactions: StubTransactions | None = None,
    publisher = CollectingPublisher()
    """
    entries = StubEntries(totals=Totals(2_000_000, 2_000_000))
    duplicates = [DuplicateRow("settle:sb_QK:ri_X", 2, "USD", "txn_2")]
    service, _ = _audit(
        entries=entries,
        transactions=StubTransactions(duplicates),
        sessions=sessions_factory,
    )

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
    """
    balances = StubBalances([CacheRow("mer_drift", "USD", 5_000)])
    entries = StubEntries(recomputed={"merchant_payable": 4_180})
    service, publisher = _audit(
        entries=entries, balances=balances, sessions=sessions_factory
    )

    def __init__(self, rows: dict[str, Record] | None = None) -> None:
        self.rows = rows or {}

    async def post(self, session: Any, **kwargs: Any) -> Any:
        self.posts.append(kwargs)
        transaction = type("Txn", (), {"id": "txn_adj_1"})()
        return type("PostResult", (), {"transaction": transaction, "created": True})()


def _adjustments(rows: dict[str, Record] | None = None):
    requests = StubRequests(rows)
    """Dual control. `chk_adjustment_dual_control` says the same thing in the schema."""
    record = Record(requested_by="staff_a")
    service, _, ledger = _adjustments({record.id: record})

    with pytest.raises(DualControlRequiredError):
        await service.approve(
            object(), record.id, approved_by="staff_a", approver_note="lgtm"
        )

    assert ledger.posts == [], "a rejected approval must not post"


async def test_a_second_approver_posts_the_adjustment() -> None:
