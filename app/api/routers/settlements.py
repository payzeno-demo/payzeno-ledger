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
    status_filter: Annotated[str | None, Query(alias="status")] = None,
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
    response_model=SettlementBatch,
    response_model=SettlementBatch,
    summary="List the items in a batch, scoped to one merchant",
)
async def list_items(
    sessions: SessionsDep,
    repositories: ReposDep,
    limit: PageLimit,
    merchant_id: Annotated[str, Query(min_length=8)],
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
