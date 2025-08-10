"""``GET /internal/v1/entries`` — the raw double-entry rows.

One route, one caller: payzeno-api's reporting module, which renders the account-level
drill-down behind a charge. It is the only surface that exposes ``ledger_entry``
directly; everything else works in transactions, which is the unit that balances.

Entries are append-only (trigger from migration ``0004``) so this endpoint has no write
counterpart and never will. A correction is a new transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    PageLimit,
    get_repositories,
    get_sessions,
    require_internal_service,
)
from app.api.schemas import LedgerEntry, Paginated
from app.errors import ValidationError
from app.logging import get_logger
from app.ports import SessionFactory

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/entries",
    tags=["entries"],
    dependencies=[Depends(require_internal_service)],
)

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]
ReposDep = Annotated[Any, Depends(get_repositories)]

#: An unbounded scan of ``ledger_entry`` is the single most expensive query this service
#: can be asked for — it is the largest table by an order of magnitude. The account
#: filter is mandatory and the window is capped so nobody can ask for the whole thing by
#: forgetting a parameter.
MAX_WINDOW_DAYS = 92


def _serialise(entry: Any) -> dict[str, Any]:
    return {
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


@router.get(
    "",
    response_model=Paginated[LedgerEntry],
    summary="List entries for one account over a bounded window",
)
async def list_entries(
    sessions: SessionsDep,
    repositories: ReposDep,
    limit: PageLimit,
    account_id: Annotated[str, Query(min_length=8)],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Served by ``ix_ledger_entry_account_created`` — the arc PERF index from ``0015``.

    Before that index this query was a sequential scan on a table with hundreds of
    millions of rows, and the console's account drill-down timed out for any merchant
    with real volume. The composite ``(account_id, created_at DESC)`` is what makes the
    cursor pagination below cheap: the ordering column is the second index column, so a
    page is a range scan rather than a sort.
    """
    if from_ is not None and to is not None:
        if to <= from_:
            raise ValidationError(
                "`to` must be after `from`",
                **{"from": from_.isoformat(), "to": to.isoformat()},
            )
        if (to - from_).days > MAX_WINDOW_DAYS:
            raise ValidationError(
                f"entry window may not exceed {MAX_WINDOW_DAYS} days",
                account_id=account_id,
                requested_days=(to - from_).days,
            )

    async with sessions.begin() as session:
        page = await repositories.ledger_entries.list_page(
            session,
            cursor=cursor,
            limit=limit,
            account_id=account_id,
            created_from=from_,
            created_to=to,
        )
        logger.debug(
            "entries_listed",
            account_id=account_id,
            returned=len(page.items),
            has_more=page.has_more,
        )
        return {
            "object": "list",
            "data": [_serialise(entry) for entry in page.items],
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        }
