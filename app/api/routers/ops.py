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
        merchant_id=body.merchant_id,
        reason_code=body.reason_code,
        requested_by=staff_id,
        payload = _serialise_request(row)

    logger.warning(
        "adjustment_approved",
        request_id=request_id,
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
        item_id=item_id,
    )
    metrics.increment("ReconciliationItemManuallyMatched")
    return payload
