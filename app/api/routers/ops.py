"""``/internal/v1/ops`` — the staff-only surface.

Four routes, all ``internal`` **plus** a forwarded staff claim. The ledger does not
authenticate humans; payzeno-api's ``StaffGuard`` does, and forwards the operator's id in
``X-Payzeno-Staff-Id``. What the ledger enforces is *attribution*: an adjustment with no
recorded maker and checker is the first thing an auditor asks about, and this service has
no ``audit_log`` table of its own to fall back on.

``AdjustmentPostingRule`` is reachable **only** through an approved
``ledger_adjustment_request``. There is deliberately no route that posts arbitrary entries
against merchant money with a reason string and nothing else.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, status

from app.api.deps import (
    InternalCaller,
    StaffCaller,
    get_adjustment_service,
    get_audit_service,
    get_manual_match,
    get_repositories,
    get_sessions,
    require_internal_service,
)
from app.api.routers.reconciliation import _serialise_item
from app.api.schemas import (
    ApproveAdjustmentRequest,
    LedgerAdjustmentRequest,
    ManualMatchRequest,
    ReconciliationItem,
    RequestAdjustmentRequest,
    RunTrialBalanceRequest,
    TrialBalanceResult,
)
from app.errors import ValidationError
from app.logging import get_logger
from app.metrics import metrics
from app.ports import SessionFactory
from app.services.audit import AdjustmentService, LedgerAuditService
from app.services.reconciliation.matcher import ManualMatch

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/ops",
    tags=["ops"],
    dependencies=[Depends(require_internal_service)],
)

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]
AuditDep = Annotated[LedgerAuditService, Depends(get_audit_service)]
AdjustmentsDep = Annotated[AdjustmentService, Depends(get_adjustment_service)]
ManualMatchDep = Annotated[ManualMatch, Depends(get_manual_match)]
ReposDep = Annotated[Any, Depends(get_repositories)]


def _serialise_request(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "object": "ledger_adjustment_request",
        "merchant_id": row.merchant_id,
        "currency": row.currency,
        "lines": row.lines,
        "reason_code": row.reason_code,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "posted_transaction_id": row.posted_transaction_id,
        "status": row.status,
    }


@router.post(
    "/audit/trial-balance",
    response_model=TrialBalanceResult,
    summary="Assert debits equal credits for one currency",
)
async def run_trial_balance(
    body: RunTrialBalanceRequest,
    audit: AuditDep,
    caller: InternalCaller,
) -> dict[str, Any]:
    """The same check :class:`~app.workers.ledger_audit.LedgerAuditJob` runs nightly.

    Exposed so an operator can run it against a point in time while an incident is open
    rather than waiting for 00:00 UTC. On failure ``LedgerAuditService`` publishes
    ``ledger.imbalance_detected`` and raises ``LedgerIntegrityError`` — a **500**, which
    is correct here and nowhere else in this router: an unbalanced ledger really is a
    server fault.

    No staff claim: it is a read, it names no merchant, and requiring one would stop the
    on-call SRE running it at 01:00 with only their ops credentials.
    """
    result = await audit.run_trial_balance(currency=body.currency, as_of=body.as_of)
    metrics.increment(
        "TrialBalanceRun",
        currency=body.currency,
        balanced=str(result.balanced).lower(),
    )
    logger.info(
        "trial_balance_run",
        currency=body.currency,
        balanced=result.balanced,
        delta_minor=result.delta_minor,
        caller=caller,
    )
    return {
        "object": "trial_balance",
        "currency": result.currency,
        "balanced": result.balanced,
        "delta_minor": result.delta_minor,
        "debit_total_minor": result.debit_total_minor,
        "credit_total_minor": result.credit_total_minor,
        "as_of": result.as_of,
    }


@router.post(
    "/adjustments",
    response_model=LedgerAdjustmentRequest,
    status_code=status.HTTP_201_CREATED,
    summary="Request a manual ledger adjustment (maker)",
)
async def request_adjustment(
    body: RequestAdjustmentRequest,
    sessions: SessionsDep,
    adjustments: AdjustmentsDep,
    staff_id: StaffCaller,
) -> dict[str, Any]:
    """Records the intent. **Posts nothing.**

    The request row carries the lines verbatim and sits ``pending`` until a *different*
    operator approves it. That gap is the control: whoever noticed the problem is rarely
    the person who should sign off on moving money to fix it.
    """
    if not body.lines:
        raise ValidationError("an adjustment needs at least one line")
    lines = [line.model_dump() for line in body.lines]

    async with sessions.begin() as session:
        row = await adjustments.request(
            session,
            merchant_id=body.merchant_id,
            currency=body.currency,
            lines=lines,
            reason_code=body.reason_code,
            requested_by=staff_id,
        )
        payload = _serialise_request(row)

    logger.warning(
        "adjustment_requested",
        request_id=payload["id"],
        merchant_id=body.merchant_id,
        reason_code=body.reason_code,
        requested_by=staff_id,
        line_count=len(lines),
    )
    return payload


@router.post(
    "/adjustments/{request_id}/approve",
    response_model=LedgerAdjustmentRequest,
    summary="Approve and post a manual adjustment (checker)",
)
async def approve_adjustment(
    body: ApproveAdjustmentRequest,
    sessions: SessionsDep,
    adjustments: AdjustmentsDep,
    staff_id: StaffCaller,
    request_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Raises ``DualControlRequiredError`` (403) when approver == requester.

    Enforced in ``AdjustmentService.approve`` against ``requested_by`` on the stored row,
    not against anything in this request — a check the caller could satisfy by sending a
    different header value is not a check. The database backs it up with
    ``chk_adjustment_dual_control``.
    """
    async with sessions.begin() as session:
        row = await adjustments.approve(
            session,
            request_id,
            approved_by=staff_id,
            approver_note=body.approver_note,
        )
        payload = _serialise_request(row)

    logger.warning(
        "adjustment_approved",
        request_id=request_id,
        approved_by=staff_id,
        posted_transaction_id=payload["posted_transaction_id"],
    )
    metrics.increment("LedgerAdjustmentPosted", reason_code=str(payload["reason_code"]))
    return payload


@router.post(
    "/items/{item_id}/match",
    response_model=ReconciliationItem,
    summary="Attach a charge to an orphaned item by hand",
)
async def manual_match(
    body: ManualMatchRequest,
    sessions: SessionsDep,
    matcher: ManualMatchDep,
    repositories: ReposDep,
    staff_id: StaffCaller,
    item_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """The last resort for an item the three automatic strategies could not place.

    Sets ``match_method='manual'`` and clears the orphan status so the next sweep will
    settle it. It does **not** settle the item itself — that stays with
    ``SettlementPoster``, so a hand-matched item goes through exactly the same posting
    rules, invariants and idempotency key as an automatically matched one.
    """
    async with sessions.begin() as session:
        item = await repositories.reconciliation_items.get_or_raise(session, item_id)
        if item.status == "settled":
            raise ValidationError(
                "item is already settled; re-matching would orphan its transaction",
                item_id=item_id,
                settled_transaction_id=item.settled_transaction_id,
            )

        # ManualMatch reads the charge off the item, so the operator's choice is written
        # first and the strategy is what validates it: a charge id that is not in the
        # settlement projection raises ChargeProjectionNotFoundError (404) out of
        # `charges.get_or_raise`, which is nearly always a copy/paste out of the
        # acquirer file rather than a real orphan.
        item.charge_id = body.charge_id
        outcome = await matcher.match(session, item)
        if outcome.charge_id is None:
            raise ValidationError(
                "charge could not be attached to this item",
                item_id=item_id,
                charge_id=body.charge_id,
            )
        item.charge_id = outcome.charge_id
        item.match_method = outcome.method
        item.status = "pending"
        payload = _serialise_item(item)

    logger.warning(
        "reconciliation_item_manually_matched",
        item_id=item_id,
        charge_id=body.charge_id,
        note=body.note,
        matched_by=staff_id,
    )
    metrics.increment("ReconciliationItemManuallyMatched")
    return payload
