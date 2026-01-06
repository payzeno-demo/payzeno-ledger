"""``/internal/v1/transactions`` — the write surface onto the double-entry ledger.

Two callers post here: payzeno-api (captures, refunds, dispute movements) and
**payzeno-billing-legacy** ``LedgerClient.postTransaction``, from
``MigratedInvoiceService#settleInvoice``. Everything else that moves money — settlement,
payouts, reserves — reaches :class:`~app.services.transactions.LedgerPoster` in-process
and never crosses HTTP.

Idempotency is contracted in ``api-surface.md`` §10.2 and is **not** implemented here:
``LedgerPoster.post`` owns it, keyed on ``idempotency_key`` and discriminated on
``request_fingerprint``. This module only chooses the conflict policy — ``'raise'``, so a
repeat with a diverging fingerprint surfaces as ``409 duplicate_settlement`` — and
translates the outcome into a status code.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.api.deps import (
    InternalCaller,
    PageLimit,
    get_ledger_poster,
    get_repositories,
    get_sessions,
    require_internal_service,
)
from app.api.schemas import (
    BulkPostTransactionRequest,
    BulkPostTransactionResponse,
    LedgerTransaction,
    Paginated,
    PostTransactionRequest,
    ReverseTransactionRequest,
)
from app.domain.idempotency import fingerprint_of
from app.domain.postings import PostingLine
from app.errors import ValidationError
from app.logging import get_logger
from app.ports import SessionFactory
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/transactions",
    tags=["transactions"],
    dependencies=[Depends(require_internal_service)],
)

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]
PosterDep = Annotated[LedgerPoster, Depends(get_ledger_poster)]
ReposDep = Annotated[Any, Depends(get_repositories)]


def _to_lines(req: PostTransactionRequest) -> list[PostingLine]:
    """``PostTransactionLine`` (wire) → ``PostingLine`` (domain).

    They carry the same three fields and are deliberately different types: the wire one
    is generated from ``types.ts`` and may gain fields the domain has no opinion about,
    and ``PostingRule.validate`` is written against the domain one.
    """
    return [
        PostingLine(
            account_type=line.account_type,
            direction=line.direction,
            amount_minor=line.amount_minor,
        )
        for line in req.lines
    ]


def _serialise(transaction: Any, entries: list[Any] | None = None) -> dict[str, Any]:
    """ORM row → the ``LedgerTransaction`` shape, entries included."""
    rows = entries if entries is not None else list(getattr(transaction, "entries", []))
    return {
        "id": transaction.id,
        "object": "ledger_transaction",
        "idempotency_key": transaction.idempotency_key,
        "request_fingerprint": transaction.request_fingerprint,
        "purpose": transaction.purpose,
        "merchant_id": transaction.merchant_id,
        "currency": transaction.currency,
        "livemode": transaction.livemode,
        "reference_type": transaction.reference_type,
        "reference_id": transaction.reference_id,
        "reverses_transaction_id": transaction.reverses_transaction_id,
        "created_by": transaction.created_by,
        "posted_at": transaction.posted_at,
        "entries": [
            {
                "id": entry.id,
                "object": "ledger_entry",
                "transaction_id": entry.transaction_id,
                "account_id": entry.account_id,
                "direction": entry.direction,
                "amount_minor": entry.amount_minor,
                "currency": entry.currency,
                "sequence": entry.sequence,
                "created_at": entry.created_at,
            }
            for entry in rows
        ],
    }


@router.post(
    "",
    response_model=LedgerTransaction,
    status_code=status.HTTP_201_CREATED,
    summary="Post a balanced double-entry transaction",
)
async def post_transaction(
    body: PostTransactionRequest,
    response: Response,
    sessions: SessionsDep,
    poster: PosterDep,
    caller: InternalCaller,
) -> dict[str, Any]:
    """Idempotent on ``idempotency_key``.

    A repeat with the same fingerprint returns **200** and the existing transaction; a
    repeat with a different one raises ``DuplicateSettlementError`` from inside
    ``LedgerPoster.post`` and comes back as ``409 duplicate_settlement`` with
    ``details.existing_transaction_id``. The status is set on the shared ``Response``
    rather than declared per-branch because FastAPI fixes ``status_code`` at decoration.
    """
    async with sessions.begin() as session:
        result = await poster.post(
            session,
            idempotency_key=body.idempotency_key,
            purpose=body.purpose,
            merchant_id=body.merchant_id,
            currency=body.currency,
            livemode=body.livemode,
            reference_type=body.reference_type,
            reference_id=body.reference_id,
            lines=_to_lines(body),
            created_by="system" if caller == "payzeno-api" else "admin",
            request_fingerprint=fingerprint_of(body),
            on_conflict="raise",
        )
        payload = _serialise(result.transaction)

    if not result.created:
        response.status_code = status.HTTP_200_OK
    logger.info(
        "transaction_posted_via_http",
        transaction_id=payload["id"],
        purpose=body.purpose,
        created=result.created,
        caller=caller,
    )
    return payload


@router.get("", response_model=Paginated[LedgerTransaction], summary="List transactions")
async def list_transactions(
    sessions: SessionsDep,
    repositories: ReposDep,
    limit: PageLimit,
    merchant_id: Annotated[str | None, Query()] = None,
    reference_type: Annotated[str | None, Query()] = None,
    reference_id: Annotated[str | None, Query()] = None,
    purpose: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Backs ``GET /v1/charges/:chargeId/ledger`` — ``reference_type=charge``.

    Read-only, so it goes straight to the repository. There is no rule between the query
    string and the ``WHERE`` clause, and a service that only forwarded would be a layer
    nobody could delete later.
    """
    async with sessions.begin() as session:
        page = await repositories.ledger_transactions.list_page(
            session,
            cursor=cursor,
            limit=limit,
            merchant_id=merchant_id,
            reference_type=reference_type,
            reference_id=reference_id,
            purpose=purpose,
        )
        return {
            "object": "list",
            "data": [_serialise(row) for row in page.items],
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        }


@router.get(
    "/{transaction_id}",
    response_model=LedgerTransaction,
    summary="Fetch one transaction with its entries",
)
async def get_transaction(
    sessions: SessionsDep,
    repositories: ReposDep,
    transaction_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Raises ``TransactionNotFoundError`` → ``404 not_found`` from ``get_or_raise``."""
    async with sessions.begin() as session:
        transaction = await repositories.ledger_transactions.get_or_raise(
            session, transaction_id
        )
        entries = await repositories.ledger_entries.list_for_transaction(
            session, transaction_id
        )
        return _serialise(transaction, entries)


@router.post(
    "/{transaction_id}/reverse",
    response_model=LedgerTransaction,
    status_code=status.HTTP_201_CREATED,
    summary="Post the mirror-image transaction that undoes another",
)
async def reverse_transaction(
    body: ReverseTransactionRequest,
    sessions: SessionsDep,
    poster: PosterDep,
    repositories: ReposDep,
    caller: InternalCaller,
    transaction_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """A reversal is a *new* transaction, never an update.

    ``ledger_entry`` has an append-only trigger from migration ``0004``; nothing in this
    repository can amend a posted entry, and that is the property the whole audit story
    rests on.
    """
    async with sessions.begin() as session:
        original = await repositories.ledger_transactions.get_or_raise(
            session, transaction_id
        )
        if original.reverses_transaction_id is not None:
            raise ValidationError(
                "cannot reverse a reversal",
                transaction_id=transaction_id,
                reverses=original.reverses_transaction_id,
            )
        result = await poster.reverse(
            session,
            original=original,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            created_by="admin",
        )
        payload = _serialise(result.transaction)

    logger.warning(
        "transaction_reversed",
        original_id=transaction_id,
        reversal_id=payload["id"],
        reason=body.reason,
        caller=caller,
    )
    return payload


@router.post(
    "/bulk",
    response_model=BulkPostTransactionResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
    summary="Post many transactions in one request",
)
async def post_transactions_bulk(
    body: BulkPostTransactionRequest,
    sessions: SessionsDep,
    poster: PosterDep,
    caller: InternalCaller,
) -> dict[str, Any]:
    """Added in month 2 for the initial ledger backfill off the Java service.

    Nothing has called it since month 5 — the backfill finished and arc MIG moved to
    per-invoice posting. It is excluded from the OpenAPI snapshot and left in place
    because the one thing worse than an unused route is discovering during a cutover
    that you deleted the route the cutover needed.

    All-or-nothing: one session, one transaction, one rollback.
    """
    posted_ids: list[str] = []
    async with sessions.begin() as session:
        for req in body.transactions:
            result = await poster.post(
                session,
                idempotency_key=req.idempotency_key,
                purpose=req.purpose,
                merchant_id=req.merchant_id,
                currency=req.currency,
                livemode=req.livemode,
                reference_type=req.reference_type,
                reference_id=req.reference_id,
                lines=_to_lines(req),
                created_by="system",
                request_fingerprint=fingerprint_of(req),
                on_conflict="return_existing",
            )
            posted_ids.append(result.transaction.id)

    logger.info("transactions_bulk_posted", count=len(posted_ids), caller=caller)
    return {"posted": len(posted_ids), "transaction_ids": posted_ids}
