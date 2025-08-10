"""`app/api/routers/transactions.py` and `entries.py` — api-surface.md §10.2.

`POST /internal/v1/transactions` is idempotent on `idempotency_key`, and the
discriminator is `ledger_transaction.request_fingerprint` — a sha256 over the
canonicalised request minus the key itself, added by migration `0020` alongside the unique
index. Without a stored fingerprint there is nothing to compare and the contract is
unimplementable:

* repeat, same fingerprint      → 200 and the existing transaction
* repeat, different fingerprint → 409 `duplicate_settlement`, with
                                  `details.existing_transaction_id`

Also here: `POST /internal/v1/transactions/bulk`. Added in month 2, called by nothing
since month 5, never deleted. It has a test because it is still routed, and a route with
no test is a route that breaks silently the next time somebody touches the router.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import Response, status

from app.api.routers.transactions import (
    get_transaction,
    list_transactions,
    post_transaction,
    post_transactions_bulk,
    reverse_transaction,
)
from app.errors import (
    DuplicateSettlementError,
    TransactionNotFoundError,
    UnbalancedTransactionError,
)
from app.repositories.base import Page

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 15, 0, tzinfo=UTC)

#: `created_by` is derived from this: `payzeno-api` posts as `system`, a human operator
#: through the ops console posts as `admin`.
CALLER = "payzeno-api"


def _transaction(txn_id: str = "txn_1", *, fingerprint: str = "fp_a") -> Any:
    return type(
        "LedgerTransaction",
        (),
        {
            "id": txn_id,
            "idempotency_key": "settle:sb_1:ri_1",
            "request_fingerprint": fingerprint,
            "purpose": "settle",
            "merchant_id": "mer_api",
            "currency": "USD",
            "livemode": True,
            "reference_type": "reconciliation_item",
            "reference_id": "ri_1",
            "created_by": "reconciliation",
            "reverses_transaction_id": None,
            "posted_at": NOW,
            "entries": [],
        },
    )()


class StubLedger:
    """`LedgerPoster` with an in-memory idempotency map and the real conflict rules."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.by_key: dict[str, Any] = {}
        self.raises = raises
        self.posts: list[dict[str, Any]] = []
        self.reversals: list[tuple[str, str, str]] = []
        self._seq = 0

    async def list_for_transaction(self, session: Any, transaction_id: str) -> list[Any]:
        self.reads.append(transaction_id)
        return self.rows


class StubRepositories:
    response = Response()

    transaction = await post_transaction(_body(), response, sessions_factory, ledger, CALLER)

    assert transaction["purpose"] == "settle"
    assert response.status_code == status.HTTP_201_CREATED
    assert ledger.posts


async def test_a_replay_with_the_same_fingerprint_returns_the_existing_row(
    sessions_factory,
) -> None:
    """200, not 201, and not a second transaction.

    payzeno-api retries this on a read timeout. Without the replay branch every timeout
    becomes a duplicate posting, which is PAY-2041 with a different trigger.
    """
    ledger = StubLedger()
    body = _body()

    replayed = Response()

    second = await post_transaction(body, replayed, sessions_factory, ledger, CALLER)

    assert first["id"] == second["id"]
    assert replayed.status_code == status.HTTP_200_OK
    assert len(ledger.posts) == 1


async def test_a_replay_with_a_different_body_is_409(sessions_factory) -> None:
    """Same key, different money. Somebody's client is reusing keys.

    Silently returning the first transaction would tell them it worked, and the second
    amount would never be booked.
    """
    ledger = StubLedger()
    await post_transaction(_body(), Response(), sessions_factory, ledger, CALLER)

    transactions = StubTransactions({"txn_1": _transaction()})

    page = await list_transactions(
        sessions_factory,
        StubRepositories(transactions=transactions),
        50,
        "mer_api",
        "reconciliation_item",
        "ri_1",
        "settle",
        None,
    )

    assert page["has_more"] is False
    assert transactions.filters[0]["merchant_id"] == "mer_api"
    assert transactions.filters[0]["purpose"] == "settle"


async def test_reversing_posts_a_compensating_transaction(sessions_factory) -> None:
