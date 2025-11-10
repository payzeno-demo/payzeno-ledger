"""`LedgerAuditJob` — app/workers/ledger_audit.py.

Nightly, and the job with the most humbling history in this repository: it ran on the
night of PAY-2041 and it passed. Every check it had was a balance check, and a duplicate
settlement is internally balanced — two transactions, six legs, debits equal credits.

PAY-2054 added the check that would have caught it. The order in which this job runs its
checks is now: trial balance (invariant 2), duplicate settlements (invariant 1), balance
cache drift (invariant 3), and — since PAY-2060 — unmatched capture attempts (invariant 6).
A failure in one must not stop the others, because the whole point of a nightly audit is
that you learn everything that is wrong in one pass rather than one thing per night.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.errors import LedgerIntegrityError
from app.workers.ledger_audit import LedgerAuditJob
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 3, 0, tzinfo=UTC)


class StubAudit:
    def __init__(
        self,
        *,
        trial_balance_raises: Exception | None = None,
        duplicates: int = 0,
        drift: int = 0,
        unmatched_captures: int = 0,
    ) -> None:
        self.trial_balance_raises = trial_balance_raises
        self.duplicates = duplicates
        self.drift = drift
        self.unmatched_captures = unmatched_captures
        self.checks: list[str] = []
        self.currencies: list[str] = []

    async def run_trial_balance(self, *, currency: str, as_of=None):
        self.checks.append("trial_balance")
        self.currencies.append(currency)
        if self.trial_balance_raises is not None:
            raise self.trial_balance_raises
        return type(
            "TrialBalanceResult",
            (),
            {"currency": currency, "balanced": True, "delta_minor": 0},
        )()

    async def check_balance_cache_drift(self, *, limit: int = 500) -> int:
        self.checks.append("balance_cache_drift")
        return self.drift

    def __init__(self, *, enabled: bool = True) -> None:
        self.ledger_audit_enabled = enabled
        self.ledger_audit_interval_seconds = 86400
        self.ledger_audit_currencies = ("USD", "EUR", "GBP")


def _job(audit: StubAudit, *, enabled: bool = True) -> LedgerAuditJob:
    return LedgerAuditJob(
        audit=audit, settings=Settings(enabled=enabled)
    )


async def test_interval_is_daily() -> None:
    job = _job(audit)

    result = await job.run_once()

    assert "duplicate_settlements" in audit.checks
    assert "balance_cache_drift" in audit.checks
    assert result.error is not None


async def test_duplicates_are_counted_into_the_pass(sessions_factory) -> None:
    audit = StubAudit(duplicates=1_847)
