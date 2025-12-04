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

    async def post(self, session: Any, **kwargs: Any) -> Any:
        if self.raises is not None:
            raise self.raises
        key = kwargs["idempotency_key"]
        existing = self.by_key.get(key)
        if existing is not None:
            if existing.request_fingerprint != kwargs["request_fingerprint"]:
                raise DuplicateSettlementError(
                    "idempotency key reused with a different body",
                    existing_transaction_id=existing.id,
                )
            return type("PostResult", (), {"transaction": existing, "created": False})()

        self._seq += 1
        transaction = _transaction(
            f"txn_{self._seq}", fingerprint=kwargs["request_fingerprint"]
        )
        transaction.idempotency_key = key
        self.by_key[key] = transaction
        self.posts.append(kwargs)
        return type("PostResult", (), {"transaction": transaction, "created": True})()

    async def reverse(
        self,
        session: Any,
        *,
        original: Any,
        reason: str,
        idempotency_key: str,
        created_by: str,
    ) -> Any:
        """The mirror-image posting. `LedgerPoster` is still the only writer.

        It takes the loaded `original` rather than an id because it needs the currency,
        the livemode flag and the entries to invert, and re-reading them inside the
        poster would be a second query for a row the route already has open.
        """
        self.reversals.append((original.id, reason, idempotency_key))
        reversal = _transaction("txn_rev_1")
        reversal.purpose = "reversal"
        reversal.reference_id = original.id
        reversal.reverses_transaction_id = original.id
        reversal.created_by = created_by
        return type("PostResult", (), {"transaction": reversal, "created": True})()


class StubTransactions:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = rows or {}
        self.filters: list[dict[str, Any]] = []

    async def list_page(self, session: Any, *, cursor: str | None, limit: int, **filters: Any) -> Page:
        self.filters.append(filters)
        return Page(items=list(self.rows.values()), next_cursor=None, has_more=False)


class StubEntries:
    """`LedgerEntryRepository` seen from this router: entries for one transaction."""

    async def list_for_transaction(self, session: Any, transaction_id: str) -> list[Any]:
        self.reads.append(transaction_id)
        return self.rows


class StubRepositories:
    def __init__(
        self,
        *,
        transactions: "StubTransactions | None" = None,
        entries: StubEntries | None = None,
    ) -> None:
        self.ledger_transactions = transactions or StubTransactions()
        self.ledger_entries = entries or StubEntries()


def _body(**kwargs: Any) -> Any:
    ledger = StubLedger()

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

    conflicting = _body(
        lines=[
            {"account_type": "acquirer_receivable", "direction": "debit", "amount_minor": 1},
            {"account_type": "merchant_payable", "direction": "credit", "amount_minor": 1},
        ]
    )

    with pytest.raises(DuplicateSettlementError) as excinfo:
        await post_transaction(conflicting, Response(), sessions_factory, ledger, CALLER)

    assert excinfo.value.http_status == 409
    assert excinfo.value.details.get("existing_transaction_id")


async def test_an_unbalanced_body_is_rejected_before_anything_is_written(
    sessions_factory,
) -> None:
    """Invariant 1. The caller gets 500 `ledger_imbalance` and no partial write."""
    ledger = StubLedger(raises=UnbalancedTransactionError("debits != credits"))

    with pytest.raises(UnbalancedTransactionError):
        await post_transaction(_body(), Response(), sessions_factory, ledger, CALLER)


# --------------------------------------------------------------------------------------
# GET / reverse
# --------------------------------------------------------------------------------------


async def test_getting_a_transaction(sessions_factory) -> None:
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
    reversal = await reverse_transaction(
        type("ReverseRequest", (), {"reason": "operator_error", "idempotency_key": "rev_1"})(),
        sessions_factory,
        ledger,
        repositories,
        CALLER,
        "txn_1",
    )

    assert reversal["purpose"] == "reversal"
    assert reversal["reverses_transaction_id"] == "txn_1"
    assert ledger.reversals == [("txn_1", "operator_error", "rev_1")]


async def test_reversing_an_unknown_transaction_is_a_404(sessions_factory) -> None:
    ledger = StubLedger()

    with pytest.raises(TransactionNotFoundError):
        await reverse_transaction(
            type("ReverseRequest", (), {"reason": "typo", "idempotency_key": "rev_2"})(),
            sessions_factory,
            ledger,
            StubRepositories(),
            CALLER,
            "txn_ghost",
        )


# --------------------------------------------------------------------------------------
# the route nobody calls any more
# --------------------------------------------------------------------------------------


async def test_the_bulk_route_still_works(sessions_factory) -> None:
    """Added month 2 for a backfill, unused since month 5, never removed.

    It is still mounted, so it is still tested. Deleting it needs a conversation with
    whoever wrote the backfill script, and that conversation has not happened.
    response = await post_transactions_bulk(
        type(
            "BulkRequest",
            (),
            {"transactions": [_body(), _body(idempotency_key="settle:sb_1:ri_2")]},
        )(),
        sessions_factory,
        ledger,
        CALLER,
    )

    assert response["posted"] == 2
    assert len(response["transaction_ids"]) == 2
