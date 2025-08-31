"""`app/api/routers/ops.py` — api-surface.md §10.5.

Four routes, all `internal` **plus** a forwarded staff claim. The ledger does not
authenticate humans — payzeno-api's `StaffGuard` does, and forwards the operator's id in
`X-Payzeno-Staff-Id`, which arrives here as a plain `str` through `StaffCaller`. What the
ledger enforces is *attribution*: `AdjustmentPostingRule` is reachable only through an
approved `ledger_adjustment_request`, and an adjustment with no recorded maker and checker
is the first thing an auditor asks about. This service has no `audit_log` table of its own
to fall back on.

Three properties get the most attention here, and all three are about who did what:

* the requester comes from the claim, never from the body — a body-supplied approver is a
  body-supplied approval;
* the approver is checked against the *stored* `requested_by`, so a caller cannot satisfy
  the control by sending a different header on the second call;
* a manual match records `match_method='manual'` and puts the item back to `pending`
  rather than settling it, so a hand-matched item still goes through `SettlementPoster`
  and the same idempotency key as every other item.

The health routes used to be tested from this file because §10.5 groups them together.
They moved to `tests/api/test_health.py` when `/readyz` grew a real database round-trip —
they have nothing in common with these four beyond a section number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.routers.ops import (
    approve_adjustment,
    manual_match,
    request_adjustment,
    run_trial_balance,
)
from app.errors import (
    DualControlRequiredError,
    LedgerIntegrityError,
    ValidationError,
)
from app.services.audit import TrialBalanceResult
from tests.factories import make_item

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 3, 0, tzinfo=UTC)

CALLER = "payzeno-api"
STAFF = "usr_staff_1"


class _RenderableResult(TrialBalanceResult):
    """`TrialBalanceResult` plus the two names the router renders.

    The service calls them `debit_minor` / `credit_minor` and `ops.py` renders them as
    `debit_total_minor` / `credit_total_minor`, because the API field names were frozen in
    `payzeno_contracts` before the service existed. The double carries both spellings
    rather than pretending the mismatch is not there; aligning them is PAY-2061 and it is a
    contracts release, not a ledger one.
    """

    @property
    def credit_total_minor(self) -> int:
        return self.credit_minor


class StubAudit:
    """`LedgerAuditService.run_trial_balance` — one coroutine, one result."""

    def __init__(self, *, raises: Exception | None = None, balanced: bool = True) -> None:
        self.raises = raises
        self.balanced = balanced
        self.calls: list[tuple[str, datetime | None]] = []

    def __init__(self, **fields: Any) -> None:
        self.fields = fields

    *,
    status: str = "pending",
    async def request(self, session: Any, **kwargs: Any) -> Any:
        self.requested.append(kwargs)
        return _adjustment_row(requested_by=kwargs["requested_by"])

    def __init__(self, *, resolves_to: str | None = "ch_1") -> None:
        self.resolves_to = resolves_to
        self.seen: list[str] = []

    async def match(self, session: Any, item: Any) -> Any:
        self.seen.append(item.id)
        return type(
            "MatchOutcome",
            (),
            {"charge_id": self.resolves_to, "method": "manual", "confidence": 1.0},
        )()


class StubItemRepository:
    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows

    def __init__(self, items: StubItemRepository) -> None:
        self.reconciliation_items = items


def _body(**kwargs: Any) -> Any:
    return type("Body", (), kwargs)()


def _orphan(item_id: str = "ri_orphan_1") -> Any:
    item = make_item(item_id=item_id, batch_id="sb_QK", charge_id=None, status="orphaned")
    item.match_method = None
    return item


# --------------------------------------------------------------------------------------
# ops — trial balance
# --------------------------------------------------------------------------------------


async def test_trial_balance_renders_the_result() -> None:
    audit = StubAudit()

    body = await run_trial_balance(_body(currency="USD", as_of=None), audit, CALLER)

    assert body["balanced"] is False
    assert body["delta_minor"] == 4_180
    assert body["debit_total_minor"] == 1_000_000


async def test_a_failing_trial_balance_is_a_500_ledger_imbalance() -> None:
    """The one error in this service that really is a server fault.

    Everything else that looks like a 500 — a frozen account, a locked settlement — is a
    business state and has a 4xx. This one means the books do not balance, and it is
    exactly the check that *passed* on the night of PAY-2041, because a duplicate
    settlement is internally balanced.
    """
    audit = StubAudit(raises=LedgerIntegrityError("out by 4180 minor units"))

    with pytest.raises(LedgerIntegrityError) as excinfo:
        await run_trial_balance(_body(currency="USD", as_of=None), audit, CALLER)

    assert excinfo.value.http_status == 500
    assert excinfo.value.code == "ledger_imbalance"


# --------------------------------------------------------------------------------------
# ops — adjustments, and the maker/checker rule
# --------------------------------------------------------------------------------------


async def test_requesting_an_adjustment_records_the_requester(sessions_factory) -> None:
    """
    adjustments = StubAdjustments()

    payload = await request_adjustment(
        _body(
            merchant_id="mer_api",
            currency="USD",
            lines=[Line(account_type="reserve", direction="debit", amount_minor=500)],
            reason_code="reserve_correction",
        ),
        sessions_factory,
        adjustments,
        "usr_staff_a",
    )

    assert payload["posted_transaction_id"] is None
    assert payload["approved_by"] is None


async def test_the_lines_are_dumped_not_passed_as_models(sessions_factory) -> None:
    adjustments = StubAdjustments()

    with pytest.raises(ValidationError):
        await request_adjustment(
            _body(merchant_id="mer_api", currency="USD", lines=[], reason_code="goodwill"),
            sessions_factory,
            adjustments,
            "usr_staff_a",
        )

    assert adjustments.requested == []
    assert sessions_factory.begin_count == 0


async def test_the_requester_cannot_approve_their_own(sessions_factory) -> None:
    adjustments = StubAdjustments()

    """
    item = _orphan()
    matcher = StubManualMatch()

    """
    item = _orphan()

    payload = await manual_match(
        _body(charge_id="ch_1", note="from the portal"),
        sessions_factory,
        StubManualMatch(),
        StubRepositories(StubItemRepository({item.id: item})),
        STAFF,
        item.id,
    )

    assert payload["status"] == "pending"
    assert payload["settled_transaction_id"] is None


async def test_a_settled_item_cannot_be_rematched(sessions_factory) -> None:
    """Re-matching it would orphan the transaction that already references it.

    Asked for twice by support during PAY-2041, and refused twice: the answer to a
    duplicate is a reversal, not a re-pointed item.
    """
    item = make_item(
        item_id="ri_settled_1",
        batch_id="sb_QK",
        charge_id="ch_9",
        status="settled",
        settled_transaction_id="txn_1",
    )

    with pytest.raises(ValidationError) as excinfo:
        await manual_match(
            _body(charge_id="ch_1", note="please"),
            sessions_factory,
            StubManualMatch(),
            StubRepositories(StubItemRepository({item.id: item})),
            STAFF,
            item.id,
        )

    assert excinfo.value.details["settled_transaction_id"] == "txn_1"


async def test_a_charge_the_strategy_cannot_place_is_a_422(sessions_factory) -> None:
    """
    item = _orphan()

    await manual_match(
        _body(charge_id="ch_1", note="from the portal"),
        sessions_factory,
        StubManualMatch(),
        StubRepositories(StubItemRepository({item.id: item})),
        STAFF,
        item.id,
    )

    assert sessions_factory.begin_count == 1
    assert sessions_factory.last.committed is True
