"""``/internal/v1/reconciliation`` — runs, per-item retry, backlog.

**This module is one of the two entry points into the settlement path.** The other is
:class:`~app.workers.retry_drain.RetryDrainJob`, and both reach the *same*
:class:`~app.services.reconciliation.retry.RetryScheduler` instance, which holds the
*same* :class:`~app.services.reconciliation.poster.SettlementPoster` the 900s sweep uses.
That sharing is deliberate — one settlement implementation, not three — and it is why the
console's retry button and a scheduled sweep can be in the poster at the same moment.

Route → service and nothing else. Every decision below (is the item claimable, has it
exhausted its attempts, is the batch locked) belongs to the service; the route's whole
job is turning the service's return value into a status code.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import (
    InternalCaller,
    get_backlog_service,
    get_locks,
    get_reconciliation_service,
    get_repositories,
    get_retry_scheduler,
    get_sessions,
    require_internal_service,
)
from app.api.schemas import (
    ReconciliationBacklog,
    ReconciliationItem,
    ReconciliationRun,
    RetryReconciliationItemRequest,
    StartReconciliationRequest,
)
from app.db.locks import AdvisoryLockManager
from app.errors import SettlementLockedError
from app.logging import get_logger
from app.metrics import metrics
from app.ports import SessionFactory
from app.services.reconciliation.backlog import BacklogService
from app.services.reconciliation.reconciler import ReconciliationService
from app.services.reconciliation.retry import RetryScheduler

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/reconciliation",
    dependencies=[Depends(require_internal_service)],
)

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]
LocksDep = Annotated[AdvisoryLockManager, Depends(get_locks)]
ReconcilerDep = Annotated[ReconciliationService, Depends(get_reconciliation_service)]
RetriesDep = Annotated[RetryScheduler, Depends(get_retry_scheduler)]
BacklogDep = Annotated[BacklogService, Depends(get_backlog_service)]
ReposDep = Annotated[Any, Depends(get_repositories)]


def _serialise_item(item: Any) -> dict[str, Any]:
    """ORM row → the ``ReconciliationItem`` shape in ``payzeno_contracts.types``."""
    return {
        "id": item.id,
        "object": "reconciliation_item",
        "batch_id": item.batch_id,
        "charge_id": item.charge_id,
        "merchant_id": item.merchant_id,
        "line_type": item.line_type,
        "gross_minor": item.gross_minor,
        "fee_minor": item.fee_minor,
        "interchange_minor": item.interchange_minor,
        "scheme_fee_minor": item.scheme_fee_minor,
        "net_minor": item.net_minor,
        "expected_gross_minor": item.expected_gross_minor,
        "variance_minor": item.variance_minor,
        "currency": item.currency,
        "livemode": item.livemode,
        "acquirer_reference": item.acquirer_reference,
        "match_method": item.match_method,
        "matched_at": item.matched_at,
        "status": item.status,
        "attempt_count": item.attempt_count,
        "last_error_code": item.last_error_code,
        "last_attempt_at": item.last_attempt_at,
        "next_attempt_at": item.next_attempt_at,
        "settled_transaction_id": item.settled_transaction_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _serialise_run(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "object": "reconciliation_run",
        "batch_id": run.batch_id,
        "trigger": run.trigger,
        "status": run.status,
        "items_total": run.items_total,
        "items_settled": run.items_settled,
        "items_failed": run.items_failed,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error_summary": run.error_summary,
    }


@router.post(
    "/runs",
    response_model=ReconciliationRun,
    response_model=ReconciliationRun,
    summary="Fetch one reconciliation run",
)
async def get_run(
    sessions: SessionsDep,
    repositories: ReposDep,
    status_code=status.HTTP_202_ACCEPTED,
    item_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Reachable from the admin console (``POST /v1/settlements/:batchId/items/:itemId/retry``)
    and from :class:`~app.workers.retry_drain.RetryDrainJob`. Both land on the same
    :meth:`RetryScheduler.retry_item`.

    ``retry_item`` returning ``None`` is a **no-op, not a failure**: the item was not in
    ``RETRYABLE_STATUSES`` when the claim ran, which usually means something else settled
    it first. The route renders that as ``409 settlement_locked`` carrying the item's
    current state under ``details.item``, and the console's ``useRetrySettlementItem()``
    shows "already settling" and refetches rather than surfacing an error toast. Its rate
    is a signal to watch, not an error budget to burn.
