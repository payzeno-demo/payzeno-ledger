"""``/internal/v1/accounts`` — the chart of accounts, per merchant.

Called by payzeno-api's ``LedgerHttpClient.bootstrapAccounts`` immediately after
``merchant.created``, and by risk when it freezes a merchant's money.

Handlers are thin by contract: open a transaction, call
:class:`~app.services.accounts.AccountResolver`, hand back what it returns. Every
business rule — the upsert on ``uq_account_merchant_type_currency_livemode``, which
account types a merchant gets, what freezing means — lives in the service, and a rule
that leaks up here is invisible to the consumers and the workers that also need it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import (
    InternalCaller,
    get_account_resolver,
    get_sessions,
    require_internal_service,
)
from app.api.schemas import (
    Account,
    AccountListResponse,
    BootstrapAccountsRequest,
    BootstrapAccountsResponse,
    FreezeAccountRequest,
)
from app.logging import get_logger
from app.ports import SessionFactory
from app.services.accounts import AccountResolver

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_internal_service)],
)

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]
ResolverDep = Annotated[AccountResolver, Depends(get_account_resolver)]


def _serialise(account: Any) -> dict[str, Any]:
    """ORM row → the ``Account`` shape in ``payzeno_contracts.types``.

    Written out rather than leaning on ``from_attributes``: the contract model is
    generated from TypeScript and the ledger's column names are not guaranteed to keep
    matching it forever. When they diverge, this function is where it is obvious.
    """
    return {
        "id": account.id,
        "object": "account",
        "merchant_id": account.merchant_id,
        "type": account.type,
        "currency": account.currency,
        "status": account.status,
        "livemode": account.livemode,
        "created_at": account.created_at,
    }


@router.post(
    "/bootstrap",
    response_model=BootstrapAccountsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create the standing accounts for a merchant/currency pair",
)
async def bootstrap_accounts(
    body: BootstrapAccountsRequest,
    sessions: SessionsDep,
    resolver: ResolverDep,
    caller: InternalCaller,
) -> dict[str, Any]:
    """Idempotent. Re-running it returns the same accounts with ``created`` at zero.

    payzeno-api calls this inside its own merchant-creation transaction and retries on
    any 5xx, so a second call is the normal case rather than the exceptional one.
    """
    started_at = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        accounts = await resolver.bootstrap(
            session,
            merchant_id=body.merchant_id,
            currency=body.currency,
            livemode=body.livemode,
        )
        # `created` is derived rather than returned by the resolver: `get_or_create`
        # upserts and has no reason to care which branch it took. An account whose
        # `created_at` post-dates the start of this request is one we just made.
        created = sum(
            1
            for account in accounts
            if account.created_at is not None and account.created_at >= started_at
        )
        payload = {
            "accounts": [_serialise(account) for account in accounts],
            "created": created,
        }
    logger.info(
        "accounts_bootstrapped",
        merchant_id=body.merchant_id,
        currency=body.currency,
        livemode=body.livemode,
        count=len(payload["accounts"]),
        caller=caller,
    )
    return payload


@router.get(
    "",
    response_model=AccountListResponse,
    summary="List accounts, optionally filtered",
)
async def list_accounts(
    sessions: SessionsDep,
    resolver: ResolverDep,
    merchant_id: Annotated[str | None, Query()] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    type: Annotated[str | None, Query(alias="type")] = None,
    livemode: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Unpaginated on purpose — a merchant has at most a dozen accounts per currency.

    ``merchant_id=None`` selects the platform-level accounts (``pix_account_platform``),
    which is what the trial balance and the ops CLI ask for.
    """
    async with sessions.begin() as session:
        accounts = await resolver.list_accounts(
            session,
            merchant_id=merchant_id,
            currency=currency,
            type_=type,
            livemode=livemode,
        )
        return {"data": [_serialise(account) for account in accounts]}


@router.post(
    "/{account_id}/freeze",
    response_model=Account,
    summary="Freeze an account so nothing may post against it",
)
async def freeze_account(
    body: FreezeAccountRequest,
    sessions: SessionsDep,
    resolver: ResolverDep,
    caller: InternalCaller,
    account_id: Annotated[str, Path(min_length=8)],
) -> dict[str, Any]:
    """Risk-initiated. Every subsequent ``LedgerPoster.post`` against it raises
    ``AccountFrozenError`` — **409**, not 500. It is a deliberate business state and a
    500 here trips payzeno-api's circuit breaker for what is a policy decision.
    """
    async with sessions.begin() as session:
        account = await resolver.freeze(session, account_id, reason=body.reason)
        payload = _serialise(account)
    logger.warning(
        "account_frozen", account_id=account_id, reason=body.reason, caller=caller
    )
    return payload
