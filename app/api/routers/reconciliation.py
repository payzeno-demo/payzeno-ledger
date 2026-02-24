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
    tags=["reconciliation"],
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
    summary="Reconcile one settlement batch now",
)
async def start_run(
    body: StartReconciliationRequest,
    sessions: SessionsDep,
    locks: LocksDep,
    reconciler: ReconcilerDep,
    caller: InternalCaller,
) -> dict[str, Any]:
    """Manual trigger for the same pass the 900s sweep runs.

    The lock probe below is a **pre-flight check, not the lock itself**. It opens a short
    transaction, tries the batch advisory lock, and lets the transaction close — which
    releases it, because every lock in ``app/db/locks.py`` is ``pg_advisory_xact_lock``
    and dies with its transaction. ``reconcile_batch`` then takes the real lock on its
    own guard session. Probing first is what turns "an operator clicked reconcile while
    the sweep was mid-batch" into a clean ``409 settlement_locked`` instead of a request
    that blocks for four minutes and then times out at the load balancer.

    It blocks for the length of the pass. That is wrong for a 202 and it is why nothing
    but the ops console calls it — PAY-1912 is open to move the pass onto the scheduler
    and hand back the run row immediately.
    """
    async with sessions.begin() as probe:
        acquired = await locks.try_acquire_batch_lock(probe, body.batch_id)
        if not acquired:
            metrics.increment("ReconciliationRunRejected", reason="batch_locked")
            raise SettlementLockedError(
                f"batch {body.batch_id} is already being reconciled",
                batch_id=body.batch_id,
            )

    logger.info(
        "reconciliation_run_requested",
        batch_id=body.batch_id,
        trigger=body.trigger or "manual",
        caller=caller,
    )
    response_model=ReconciliationRun,
    summary="Fetch one reconciliation run",
)
async def get_run(
    sessions: SessionsDep,
    repositories: ReposDep,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry one reconciliation item",
)
async def retry_item(
    body: RetryReconciliationItemRequest,
    sessions: SessionsDep,
    retries: RetriesDep,
    repositories: ReposDep,
    caller: InternalCaller,
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
    item = await retries.retry_item(item_id, requested_by=requested_by)
    if item is not None:
        logger.info(
            "reconciliation_item_retried",
            item_id=item_id,
            status=item.status,
            attempt_count=item.attempt_count,
            requested_by=requested_by,
        )
        return _serialise_item(item)

    # Not claimable. Re-read it in its own session so the 409 carries the state the
    # caller should now render, rather than the state it had when they clicked.
    async with sessions.begin() as session:
        current = await repositories.reconciliation_items.get_or_raise(session, item_id)
        snapshot = _serialise_item(current)

    metrics.increment(
        "SettlementItemRetryRejected", status=str(snapshot["status"])
    )
    logger.info(
        "reconciliation_item_not_claimable",
        item_id=item_id,
        status=snapshot["status"],
        requested_by=requested_by,
    )
    raise SettlementLockedError(
        f"item {item_id} is {snapshot['status']} and was not claimable",
        item_id=item_id,
        item=snapshot,
    )


@router.get(
    "/backlog",
    response_model=ReconciliationBacklog,
