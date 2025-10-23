"""``/internal/v1/settlement-batches`` and ``/internal/v1/settlement-imports``.

Three consumers, three shapes of traffic:

* payzeno-api, behind the console's settlement explorer — reads only, always scoped by
  ``merchant_id``.
* :class:`~app.services.settlements.SettlementImportService`, which is the only caller of
  ``open_batch`` and ``close_batch`` even though both are exposed here. They are HTTP
  routes because the settlement-import runbook needs a way to re-open a batch by hand at
  three in the morning, not because anything calls them in the happy path.
* **payzeno-billing-legacy** ``LedgerReconciliationExportJob#run`` → ``POST
  /internal/v1/settlement-imports``. That is the legacy path, and it goes to
  ``SettlementService.import_legacy_records``, not to ``SettlementImportService`` — two
  importers, ~40 lines of duplicated matching, and no plan to converge until the Java
  service is switched off.

``merchant_id`` is **required** on ``/items`` and optional on the batch list. That
asymmetry is contracted (``api-surface.md`` §8): a batch is Payzeno's row and items are
merchant money, so listing items without a merchant scope is a cross-tenant read.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import (
    InternalCaller,
    PageLimit,
    get_funding_service,
    get_repositories,
    get_sessions,
    get_settlement_service,
    require_internal_service,
)
from app.api.routers.reconciliation import _serialise_item
from app.api.schemas import (
    ItemStatusFilter,
    OpenSettlementBatchRequest,
    Paginated,
    ReconciliationItem,
    RecordFundingRequest,
    SettlementBatch,
    SettlementImportRequest,
    SettlementImportResponse,
)
from app.logging import get_logger
from app.ports import SessionFactory
from app.services.funding import FundingMatchService
from app.services.settlements import SettlementService

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1",
    tags=["settlements"],
    dependencies=[Depends(require_internal_service)],
)

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]
SettlementsDep = Annotated[SettlementService, Depends(get_settlement_service)]
FundingDep = Annotated[FundingMatchService, Depends(get_funding_service)]
ReposDep = Annotated[Any, Depends(get_repositories)]


def _serialise_batch(batch: Any) -> dict[str, Any]:
    """ORM row → the ``SettlementBatch`` shape."""
    return {
        "id": batch.id,
        "object": "settlement_batch",
        "acquirer": batch.acquirer,
        "currency": batch.currency,
        "processing_date": batch.processing_date,
        "file_reference": batch.file_reference,
        "status": batch.status,
        "livemode": batch.livemode,
        "item_count": batch.item_count,
        "gross_minor": batch.gross_minor,
        "fee_minor": batch.fee_minor,
        "net_minor": batch.net_minor,
        "funded_minor": batch.funded_minor,
        "funded_at": batch.funded_at,
        "reconciled_at": batch.reconciled_at,
        "created_at": batch.created_at,
    }


@router.get(
    "/settlement-batches",
    response_model=Paginated[SettlementBatch],
    summary="List settlement batches",
)
async def list_batches(
    sessions: SessionsDep,
    repositories: ReposDep,
    limit: PageLimit,
    merchant_id: Annotated[str | None, Query()] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    acquirer: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """``merchant_id`` is optional here — staff and ops omit it deliberately."""
    async with sessions.begin() as session:
        page = await repositories.settlement_batches.list_page(
            session,
            cursor=cursor,
            limit=limit,
            merchant_id=merchant_id,
            currency=currency,
            status=status_filter,
            acquirer=acquirer,
        )
        return {
            "object": "list",
            "data": [_serialise_batch(row) for row in page.items],
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        }


@router.get(
    "/settlement-batches/{batch_id}",
    response_model=SettlementBatch,
    summary="Fetch one batch",
)
async def get_batch(
    sessions: SessionsDep,
    repositories: ReposDep,
    batch_id: Annotated[str, Path(min_length=8)],
    merchant_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """``merchant_id`` is accepted and ignored for the batch row itself.

    payzeno-api forwards it from the console's scope guard; the ledger does not filter a
    batch by merchant because a batch spans merchants by construction. The parameter
    stays in the signature so the caller's contract test passes and so the access log
    records who asked.
    """
    async with sessions.begin() as session:
        batch = await repositories.settlement_batches.get_or_raise(session, batch_id)
        return _serialise_batch(batch)


@router.post(
    "/settlement-batches",
    response_model=SettlementBatch,
    status_code=status.HTTP_201_CREATED,
    summary="Open a settlement batch",
)
async def open_batch(
    body: OpenSettlementBatchRequest,
    sessions: SessionsDep,
    settlements: SettlementsDep,
    caller: InternalCaller,
) -> dict[str, Any]:
    """Idempotent on ``uq_settlement_batch_file`` — re-importing the same acquirer file
    returns the existing batch rather than creating a second one, which is what makes the
    import job safe to re-run from the runbook.
    """
    async with sessions.begin() as session:
        batch = await settlements.open_batch(
            session,
            acquirer=body.acquirer,
            currency=body.currency,
            processing_date=body.processing_date,
            file_reference=body.file_reference,
            livemode=body.livemode,
        )
        payload = _serialise_batch(batch)
    logger.info(
        "settlement_batch_opened",
        batch_id=payload["id"],
        acquirer=body.acquirer,
        file_reference=body.file_reference,
        caller=caller,
    )
    return payload


@router.post(
    "/settlement-batches/{batch_id}/close",
    response_model=SettlementBatch,
    summary="Close a batch so it becomes reconcilable",
)
async def close_batch(
    sessions: SessionsDep,
    settlements: SettlementsDep,
    caller: InternalCaller,
    batch_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Raises ``BatchNotReconcilableError`` (422) on a batch that is not ``open``.

    Closing is what makes a batch visible to :class:`ReconciliationSweepJob`; until then
    the importer may still be appending items to it and reconciling half a file would
    settle half a merchant's day.
    """
    async with sessions.begin() as session:
        batch = await settlements.close_batch(session, batch_id)
        payload = _serialise_batch(batch)
    logger.info("settlement_batch_closed", batch_id=batch_id, caller=caller)
    return payload


@router.get(
    "/settlement-batches/{batch_id}/items",
    response_model=Paginated[ReconciliationItem],
    summary="List the items in a batch, scoped to one merchant",
)
async def list_items(
    sessions: SessionsDep,
    repositories: ReposDep,
    limit: PageLimit,
    batch_id: Annotated[str, Path(min_length=8)],
    merchant_id: Annotated[str, Query(min_length=8)],
    status_filter: Annotated[ItemStatusFilter | None, Query(alias="status")] = None,
    line_type: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """``merchant_id`` is **required**. It is merchant money, and an unscoped list is a
    cross-tenant read dressed up as a missing default.
    """
    async with sessions.begin() as session:
        page = await repositories.reconciliation_items.list_page(
            session,
            cursor=cursor,
            limit=limit,
            batch_id=batch_id,
            merchant_id=merchant_id,
            status=status_filter,
            line_type=line_type,
        )
        return {
            "object": "list",
            "data": [_serialise_item(row) for row in page.items],
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        }


@router.post(
    "/settlement-batches/{batch_id}/funding",
    response_model=SettlementBatch,
    summary="Record the bank credit that funded a batch",
)
async def record_funding(
    body: RecordFundingRequest,
    sessions: SessionsDep,
    funding: FundingDep,
    caller: InternalCaller,
    batch_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Matches a ``funding_event`` and posts the ``settlement_funding`` transaction.

    A fully reconciled batch is still unfunded until this fires: reconciliation says the
    acquirer *agreed* the numbers, funding says the money *arrived*, and conflating them
    is how a platform pays out against a credit that never landed.
    """
    async with sessions.begin() as session:
        batch = await funding.record_funding(
            session,
            batch_id=batch_id,
            bank_reference=body.bank_reference,
            amount_minor=body.amount_minor,
            value_date=body.value_date,
        )
        payload = _serialise_batch(batch)
    logger.info(
        "settlement_batch_funded",
        batch_id=batch_id,
        bank_reference=body.bank_reference,
        amount_minor=body.amount_minor,
        caller=caller,
    )
    return payload


@router.post(
    "/settlement-imports",
    response_model=SettlementImportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Accept a settlement file pushed by payzeno-billing-legacy",
)
async def import_legacy_records(
    body: SettlementImportRequest,
    sessions: SessionsDep,
    settlements: SettlementsDep,
    repositories: ReposDep,
    caller: InternalCaller,
) -> dict[str, Any]:
    """The Java service pushes records at us instead of us pulling the file.

    Predates :class:`SettlementImportService` and duplicates a good chunk of its matching.
    It survives because ``LedgerReconciliationExportJob`` is scheduled inside the legacy
    service's own quartz cluster and moving it is arc MIG work nobody has scheduled.
    """
    records = [record.model_dump() for record in body.records]
    async with sessions.begin() as session:
        batch_id, item_count = await settlements.import_legacy_records(
            session,
            acquirer=body.acquirer,
            processing_date=body.processing_date,
            file_reference=body.file_reference,
            records=records,
            strategies=repositories.match_strategies,
        )
    logger.info(
        "legacy_settlement_import",
        batch_id=batch_id,
        item_count=item_count,
        file_reference=body.file_reference,
        caller=caller,
    )
    return {"batch_id": batch_id, "item_count": item_count}
