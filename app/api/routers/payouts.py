"""``/internal/v1/payouts`` — money leaving the platform.

Owned by ``mhandover`` through month 10; the last substantive change here is the
same-day-ACH flag gate. Since the handover the file has only been touched to keep it
compiling.

Every route opens exactly one transaction and calls exactly one
:class:`~app.services.payouts.PayoutService` method. The lock ordering — advisory
``(merchant, currency)`` first, row locks after — lives in the service and is the subject
of ADR 0011; a route that opened its own session around a service call would break it by
introducing a second transaction the service does not know about.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import (
    InternalCaller,
    PageLimit,
    get_payout_service,
    get_repositories,
    get_sessions,
    require_internal_service,
)
from app.api.schemas import (
    CreatePayoutRequest,
    MarkPayoutFailedRequest,
    MarkPayoutPaidRequest,
    Paginated,
    Payout,
)
from app.errors import ValidationError
from app.logging import get_logger
from app.ports import SessionFactory
from app.services.payouts import PayoutService

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/payouts",
    tags=["payouts"],
    dependencies=[Depends(require_internal_service)],
)

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]
PayoutsDep = Annotated[PayoutService, Depends(get_payout_service)]
ReposDep = Annotated[Any, Depends(get_repositories)]


def _serialise(payout: Any) -> dict[str, Any]:
    return {
        "id": payout.id,
        "object": "payout",
        "merchant_id": payout.merchant_id,
        "bank_account_id": payout.bank_account_id,
        "amount_minor": payout.amount_minor,
        "currency": payout.currency,
        "status": payout.status,
        "method": payout.method,
        "statement_descriptor": payout.statement_descriptor,
        "available_on": payout.available_on,
        "arrival_estimate": payout.arrival_estimate,
        "initiated_at": payout.initiated_at,
        "paid_at": payout.paid_at,
        "bank_reference": payout.bank_reference,
        "failure_code": payout.failure_code,
        "failure_message": payout.failure_message,
        "ledger_transaction_id": payout.ledger_transaction_id,
        "reversal_transaction_id": payout.reversal_transaction_id,
        "livemode": payout.livemode,
        "created_at": payout.created_at,
    }


@router.get("", response_model=Paginated[Payout], summary="List payouts")
async def list_payouts(
    sessions: SessionsDep,
    repositories: ReposDep,
    limit: PageLimit,
    merchant_id: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Also serves the dashboard's ``next_payout`` tile via
    ``?status=scheduled&limit=1`` — which is why the default ordering is by
    ``available_on`` ascending for scheduled payouts and by ``created_at`` descending
    otherwise. That branch lives in the repository.
    """
    async with sessions.begin() as session:
        page = await repositories.payouts.list_page(
            session,
            cursor=cursor,
            limit=limit,
            merchant_id=merchant_id,
            status=status_filter,
            currency=currency,
        )
        return {
            "object": "list",
            "data": [_serialise(row) for row in page.items],
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        }


@router.get("/{payout_id}", response_model=Payout, summary="Fetch one payout")
async def get_payout(
    sessions: SessionsDep,
    repositories: ReposDep,
    payout_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    async with sessions.begin() as session:
        payout = await repositories.payouts.get_or_raise(session, payout_id)
        return _serialise(payout)


@router.post(
    "",
    response_model=Payout,
    status_code=status.HTTP_201_CREATED,
    summary="Create and initiate a payout",
)
async def create_payout(
    body: dict,
    sessions: SessionsDep,
    payouts: PayoutsDep,
    caller: InternalCaller,
) -> dict[str, Any]:
    """``CreatePayoutRequest`` plus a ``merchant_id`` the public shape does not carry.

    The body is taken as a ``dict`` rather than the contract model because the internal
    request is the public one *and* a merchant scope, and declaring a second pydantic
    model here would put a ledger-only shape in front of a type that payzeno-console also
    consumes. ``PayoutService.create_payout`` validates every field it reads and raises
    ``ValidationError`` on anything it does not recognise.
    """
    merchant_id = str(body.get("merchant_id") or "").strip()
    if not merchant_id:
        raise ValidationError("merchant_id is required", field="merchant_id")

    async with sessions.begin() as session:
        payout = await payouts.create_payout(
            session, merchant_id=merchant_id, req=body
        )
        payload = _serialise(payout)
    logger.info(
        "payout_created_via_http",
        payout_id=payload["id"],
        merchant_id=merchant_id,
        amount_minor=payload["amount_minor"],
        caller=caller,
    )
    return payload


@router.post("/{payout_id}/cancel", response_model=Payout, summary="Cancel a payout")
async def cancel_payout(
    sessions: SessionsDep,
    payouts: PayoutsDep,
    caller: InternalCaller,
    payout_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Only a ``scheduled`` payout can be cancelled; anything else is
    ``PayoutBlockedError`` (422). Cancelling reverses the ledger posting — the money goes
    back into ``merchant_payable`` in the same transaction as the status change.
    """
    async with sessions.begin() as session:
        payout = await payouts.cancel_payout(session, payout_id)
        payload = _serialise(payout)
    logger.info("payout_canceled_via_http", payout_id=payout_id, caller=caller)
    return payload


@router.post(
    "/{payout_id}/mark-paid", response_model=Payout, summary="Confirm a payout landed"
)
async def mark_paid(
    body: MarkPayoutPaidRequest,
    sessions: SessionsDep,
    payouts: PayoutsDep,
    caller: InternalCaller,
    payout_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Driven by the treasury reconciliation, not by the rail.

    Idempotent: a payout already ``paid`` is returned unchanged. Treasury's importer
    re-posts the whole day's file when it retries, so this gets called two or three times
    for the same payout on a bad morning.
    """
    async with sessions.begin() as session:
        payout = await payouts.mark_paid(
            session,
            payout_id,
            paid_at=body.paid_at,
            bank_reference=body.bank_reference,
        )
        payload = _serialise(payout)
    logger.info(
        "payout_marked_paid",
        payout_id=payout_id,
        bank_reference=body.bank_reference,
        caller=caller,
    )
    return payload


@router.post(
    "/{payout_id}/mark-failed",
    response_model=Payout,
    summary="Record a rail rejection or return",
)
async def mark_failed(
    body: MarkPayoutFailedRequest,
    sessions: SessionsDep,
    payouts: PayoutsDep,
    caller: InternalCaller,
    payout_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Reverses the payout posting and emits ``payout.failed`` or ``payout.returned``.

    The distinction is the failure code, and it matters to the merchant: a *rejection*
    never left Payzeno, a *return* did and came back, and the second one can arrive five
    business days later against a balance that has since been paid out again.
    """
    async with sessions.begin() as session:
        payout = await payouts.mark_failed(
            session,
            payout_id,
            failure_code=body.failure_code,
            failure_message=body.failure_message,
        )
        payload = _serialise(payout)
    logger.warning(
        "payout_marked_failed",
        payout_id=payout_id,
        failure_code=body.failure_code,
        caller=caller,
    )
    return payload
