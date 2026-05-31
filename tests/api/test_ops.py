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
    def debit_total_minor(self) -> int:
        return self.debit_minor

    @property
    def credit_total_minor(self) -> int:
        return self.credit_minor


class StubAudit:
    """`LedgerAuditService.run_trial_balance` — one coroutine, one result."""

    def __init__(self, *, raises: Exception | None = None, balanced: bool = True) -> None:
        self.raises = raises
        self.balanced = balanced
        self.calls: list[tuple[str, datetime | None]] = []

    async def run_trial_balance(
        self, *, currency: str, as_of: datetime | None = None
    ) -> _RenderableResult:
        self.calls.append((currency, as_of))
        if self.raises is not None:
            raise self.raises
        return _RenderableResult(
            currency=currency,
            as_of=as_of or NOW,
            debit_minor=1_000_000,
            credit_minor=1_000_000 if self.balanced else 995_820,
            balanced=self.balanced,
        )


class Line:
    """A request line. `request_adjustment` calls `model_dump()` on each one."""

    def __init__(self, **fields: Any) -> None:
        self.fields = fields

    def model_dump(self) -> dict[str, Any]:
        return dict(self.fields)


def _adjustment_row(
    request_id: str = "adj_00000001",
    *,
    status: str = "pending",
    approved_by: str | None = None,
    posted_transaction_id: str | None = None,
    merchant_id: str = "mer_api",
    requested_by: str = STAFF,
) -> Any:
    return type(
        "LedgerAdjustmentRequest",
        (),
        {
            "id": request_id,
            "merchant_id": merchant_id,
            "currency": "USD",
            "lines": [{"account_type": "merchant_payable", "direction": "credit", "amount_minor": 500}],
            "reason_code": "goodwill",
            "requested_by": requested_by,
            "requested_at": NOW,
            "approved_by": approved_by,
            "approved_at": NOW if approved_by else None,
            "posted_transaction_id": posted_transaction_id,
            "status": status,
        },
    )()


class StubAdjustments:
    def __init__(self, *, approve_raises: Exception | None = None) -> None:
        self.requested: list[dict[str, Any]] = []
        self.approved: list[tuple[str, str]] = []
        self.approve_raises = approve_raises

    async def request(self, session: Any, **kwargs: Any) -> Any:
        self.requested.append(kwargs)
        return _adjustment_row(requested_by=kwargs["requested_by"])

    async def approve(
        self, session: Any, request_id: str, *, approved_by: str, approver_note: str
    ) -> Any:
        if self.approve_raises is not None:
            raise self.approve_raises
        self.approved.append((request_id, approved_by))
        return _adjustment_row(
            request_id,
            status="posted",
            approved_by=approved_by,
            posted_transaction_id="txn_adj_1",
        )


class StubManualMatch:
    """`ManualMatch.match` — the fourth `MatchStrategy`, and the only human one."""

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

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        return self.rows[entity_id]


class StubRepositories:
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

    assert body["object"] == "trial_balance"
    assert body["currency"] == "USD"
    assert body["balanced"] is True
    assert body["delta_minor"] == 0
    assert audit.calls == [("USD", None)]


async def test_trial_balance_accepts_a_point_in_time() -> None:
    """Run against 23:59 while the incident is open, not against a moving now."""
    audit = StubAudit()

    body = await run_trial_balance(_body(currency="GBP", as_of=NOW), audit, CALLER)

    assert audit.calls == [("GBP", NOW)]
    assert body["as_of"] == NOW


async def test_an_imbalance_shows_up_as_a_signed_delta() -> None:
    """Debits minus credits. The sign says which way the books are out."""
    audit = StubAudit(balanced=False)

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
    adjustments = StubAdjustments()

    payload = await request_adjustment(
        _body(
            merchant_id="mer_api",
            currency="USD",
            lines=[
                Line(account_type="merchant_payable", direction="credit", amount_minor=500)
            ],
            reason_code="goodwill",
        ),
        sessions_factory,
        adjustments,
        "usr_staff_a",
    )

    assert payload["status"] == "pending"
    assert payload["posted_transaction_id"] is None
    assert adjustments.requested[0]["requested_by"] == "usr_staff_a"


async def test_requesting_posts_nothing(sessions_factory) -> None:
    """The request row carries the lines verbatim and sits `pending`.

    That gap is the control: whoever noticed the problem is rarely the person who should
    sign off on moving money to fix it.
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
    """They land in a `jsonb` column. A pydantic model does not serialise into one."""
    adjustments = StubAdjustments()

    await request_adjustment(
        _body(
            merchant_id="mer_api",
            currency="USD",
            lines=[Line(account_type="merchant_payable", direction="credit", amount_minor=500)],
            reason_code="goodwill",
        ),
        sessions_factory,
        adjustments,
        "usr_staff_a",
    )

    assert adjustments.requested[0]["lines"] == [
        {"account_type": "merchant_payable", "direction": "credit", "amount_minor": 500}
    ]


async def test_an_adjustment_with_no_lines_is_a_422(sessions_factory) -> None:
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
    adjustments = StubAdjustments(
        approve_raises=DualControlRequiredError("cannot approve your own request")
    )

    with pytest.raises(DualControlRequiredError) as excinfo:
        await approve_adjustment(
            _body(approver_note="lgtm"),
            sessions_factory,
            adjustments,
            "usr_staff_a",
            "adj_00000001",
        )

    assert excinfo.value.http_status == 403
    assert excinfo.value.code == "permission_denied"


async def test_a_second_approver_posts_it(sessions_factory) -> None:
    adjustments = StubAdjustments()

    payload = await approve_adjustment(
        _body(approver_note="checked against the support thread"),
        sessions_factory,
        adjustments,
        "usr_staff_b",
        "adj_00000001",
    )

    assert payload["status"] == "posted"
    assert payload["posted_transaction_id"] == "txn_adj_1"
    assert adjustments.approved == [("adj_00000001", "usr_staff_b")]


async def test_the_approver_comes_from_the_staff_claim_not_the_body(
    sessions_factory,
) -> None:
    """A body-supplied approver is a body-supplied approval.

    The claim is the only thing that has been authenticated, so it is the only thing that
    can name a human in the audit record. `chk_adjustment_dual_control` backs it up in the
    database, which is where it survives a future route that forgets.
    """
    adjustments = StubAdjustments()

    await approve_adjustment(
        _body(approver_note="ok", approved_by="usr_someone_else"),
        sessions_factory,
        adjustments,
        "usr_staff_b",
        "adj_00000001",
    )

    assert adjustments.approved == [("adj_00000001", "usr_staff_b")]


# --------------------------------------------------------------------------------------
# ops — manual match
# --------------------------------------------------------------------------------------


async def test_manual_match_links_the_item_and_records_how(sessions_factory) -> None:
    """`ManualMatch`'s only entry point.

    `match_method='manual'` is what tells the next person reading the row that a human
    decided this, and it is the reason `ManualMatch` exists as a strategy rather than as
    a bare UPDATE.
    """
    item = _orphan()
    matcher = StubManualMatch()

    payload = await manual_match(
        _body(charge_id="ch_1", note="matched from the acquirer portal"),
        sessions_factory,
        matcher,
        StubRepositories(StubItemRepository({item.id: item})),
        STAFF,
        item.id,
    )

    assert payload["charge_id"] == "ch_1"
    assert payload["match_method"] == "manual"
    assert matcher.seen == [item.id]


async def test_a_matched_item_goes_back_to_pending_not_to_settled(
    sessions_factory,
) -> None:
    """The route matches. `SettlementPoster` settles.

    A hand-matched item therefore goes through the same posting rules, the same
    invariants and the same idempotency key as an automatically matched one — which is
    the whole reason this route does not post anything itself.
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
    """Nearly always a copy/paste out of the acquirer file rather than a real orphan."""
    item = _orphan()

    with pytest.raises(ValidationError) as excinfo:
        await manual_match(
            _body(charge_id="ch_typo", note="from the portal"),
            sessions_factory,
            StubManualMatch(resolves_to=None),
            StubRepositories(StubItemRepository({item.id: item})),
            STAFF,
            item.id,
        )

    assert excinfo.value.details["charge_id"] == "ch_typo"
    assert excinfo.value.details["item_id"] == item.id


async def test_the_match_runs_in_one_transaction(sessions_factory) -> None:
    """Read, mutate, serialise — all before the session closes.

    `_serialise_item` reads twenty-odd columns off the row; doing it after the commit
    would be a lazy load against a closed session.
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
