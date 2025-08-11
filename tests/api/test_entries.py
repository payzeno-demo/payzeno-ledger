"""`app/api/routers/entries.py` — api-surface.md §10.2.

One route, one caller: payzeno-api's reporting module, behind the account-level drill-down
on a charge. It is the only surface that exposes `ledger_entry` directly, and it is
read-only forever — `ledger_entry` has an append-only trigger from migration `0004`, so a
write route here could not work even if somebody added one. `test_the_module_exposes_one_
read_route` is there to keep it that way.

The two guards are the interesting part. `ledger_entry` is the largest table in the
service by an order of magnitude, and an unbounded scan of it is the single most expensive
query this process can be asked to run — which the console discovered in month 7 by
timing out on a merchant with real volume. Hence: `account_id` is mandatory, and the
window is capped at `MAX_WINDOW_DAYS`. Both are asserted below, including the boundary,
because "92 days" that actually rejects 92 days is a support ticket.

The pagination is keyset, not offset, and this file only checks that the route hands the
cursor down and the page back. What keyset pagination *means* — that the cursor is the
last id of the previous page and that the ordering column is the second column of
`ix_ledger_entry_account_created` — belongs to `tests/repositories/test_ledger_entry.py`,
which can run it against a real index.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.api.routers import entries as entries_module
from app.api.routers.entries import MAX_WINDOW_DAYS, list_entries
from app.errors import ValidationError
from app.repositories.base import Page

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
MONTH_AGO = NOW - timedelta(days=30)


def _entry(entry_id: str, *, direction: str, amount_minor: int, sequence: int) -> Any:
    return type(
        "LedgerEntry",
        (),
        {
            "id": entry_id,
            "transaction_id": "txn_settle_1",
            "account_id": "acct_merchant_payable_usd",
            "direction": direction,
            "amount_minor": amount_minor,
            "currency": "USD",
            "sequence": sequence,
            "created_at": NOW,
        },
    )()


#: One settlement posting: gross credit to the payable, fee debit, reserve debit. Three
#: rows, balanced, in `sequence` order — the shape `uq_ledger_entry_txn_sequence` enforces.
SETTLEMENT_ENTRIES = [
    _entry("le_1", direction="credit", amount_minor=100_000, sequence=1),
    _entry("le_2", direction="debit", amount_minor=2_900, sequence=2),
    _entry("le_3", direction="debit", amount_minor=10_000, sequence=3),
]


class StubEntryRepository:
    """`LedgerEntryRepository.list_page` with a canned page behind it."""

    def __init__(self, page: Page[Any] | None = None) -> None:
        self.page = page or Page(items=list(SETTLEMENT_ENTRIES), next_cursor=None, has_more=False)
        self.calls: list[dict[str, Any]] = []

    async def list_page(self, session: Any, **kwargs: Any) -> Page[Any]:
        self.calls.append(dict(kwargs, session=repr(session)))
        return self.page


class StubRepositories:
    """Stands in for the container's repository namespace.

    The route reaches exactly one attribute off it. Anything else is a layering violation
    and would fail here with an `AttributeError` rather than in staging.
    """

    def __init__(self, ledger_entries: StubEntryRepository) -> None:
        self.ledger_entries = ledger_entries


# --------------------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------------------


async def test_entries_are_serialised_in_the_documented_shape(sessions_factory) -> None:
    repo = StubEntryRepository()

    body = await list_entries(
        sessions_factory, StubRepositories(repo), 50, "acct_merchant_payable_usd"
    )

    assert body["object"] == "list"
    assert len(body["data"]) == 3
    first = body["data"][0]
    assert first["object"] == "ledger_entry"
    assert first["transaction_id"] == "txn_settle_1"
    assert first["direction"] == "credit"
    assert first["amount_minor"] == 100_000
    assert first["sequence"] == 1


async def test_one_transaction_is_read_in_one_session(sessions_factory) -> None:
    """A read route opens one transaction and closes it. Nothing here is long-lived."""
    repo = StubEntryRepository()

    await list_entries(
        sessions_factory, StubRepositories(repo), 50, "acct_merchant_payable_usd"
    )

    assert sessions_factory.begin_count == 1
    assert sessions_factory.last.committed is True


async def test_the_account_filter_always_reaches_the_repository(sessions_factory) -> None:
    """It is mandatory at the query-parameter level; this proves it survives the handler.

    Dropping it would turn a merchant-scoped drill-down into a full-table scan that also
    returns another merchant's rows, which is the worse half of that sentence.
    """
    repo = StubEntryRepository()

    await list_entries(sessions_factory, StubRepositories(repo), 50, "acct_reserve_usd")

    assert repo.calls[0]["account_id"] == "acct_reserve_usd"


async def test_the_page_size_is_passed_down_verbatim(sessions_factory) -> None:
    """`PageLimit` has already clamped it — the handler does not clamp it twice."""
    repo = StubEntryRepository()

    await list_entries(
        sessions_factory, StubRepositories(repo), 200, "acct_merchant_payable_usd"
    )

    assert repo.calls[0]["limit"] == 200


async def test_a_cursor_continues_the_previous_page(sessions_factory) -> None:
    repo = StubEntryRepository(
        Page(items=SETTLEMENT_ENTRIES[:2], next_cursor="bGVfMg", has_more=True)
    )

    body = await list_entries(
        sessions_factory,
        StubRepositories(repo),
        2,
        "acct_merchant_payable_usd",
        None,
        None,
        "bGVfMQ",
    )

    assert repo.calls[0]["cursor"] == "bGVfMQ"
    assert body["has_more"] is True
    assert body["next_cursor"] == "bGVfMg"


async def test_the_last_page_carries_no_cursor(sessions_factory) -> None:
    """`next_cursor is None` exactly when `has_more` is False — a caller can loop on one."""
    repo = StubEntryRepository()

    body = await list_entries(
        sessions_factory, StubRepositories(repo), 50, "acct_merchant_payable_usd"
    )

    assert body["has_more"] is False
    assert body["next_cursor"] is None


async def test_an_account_with_no_entries_is_an_empty_list(sessions_factory) -> None:
    """A brand-new reserve account. Not a 404 — the account exists, it is just quiet."""
    repo = StubEntryRepository(Page(items=[], next_cursor=None, has_more=False))

    body = await list_entries(
        sessions_factory, StubRepositories(repo), 50, "acct_reserve_usd"
    )

    assert body["data"] == []
    assert body["has_more"] is False


# --------------------------------------------------------------------------------------
# the window guard
# --------------------------------------------------------------------------------------


async def test_no_window_means_no_date_filter(sessions_factory) -> None:
    repo = StubEntryRepository()

    await list_entries(
        sessions_factory, StubRepositories(repo), 50, "acct_merchant_payable_usd"
    )

    assert repo.calls[0]["created_from"] is None
    assert repo.calls[0]["created_to"] is None


async def test_a_bounded_window_is_passed_through(sessions_factory) -> None:
    repo = StubEntryRepository()

    await list_entries(
        sessions_factory,
        StubRepositories(repo),
        50,
        "acct_merchant_payable_usd",
        MONTH_AGO,
        NOW,
    )

    assert repo.calls[0]["created_from"] == MONTH_AGO
    assert repo.calls[0]["created_to"] == NOW


async def test_a_reversed_window_is_a_422(sessions_factory) -> None:
    repo = StubEntryRepository()

    with pytest.raises(ValidationError) as excinfo:
        await list_entries(
            sessions_factory,
            StubRepositories(repo),
            50,
            "acct_merchant_payable_usd",
            NOW,
            MONTH_AGO,
        )

    assert excinfo.value.http_status == 422
    assert repo.calls == []


async def test_an_over_long_window_is_refused_before_the_query_runs(sessions_factory) -> None:
    """The guard is worthless if it fires after the sequential scan has started."""
    repo = StubEntryRepository()
    far_back = NOW - timedelta(days=MAX_WINDOW_DAYS + 1)

    with pytest.raises(ValidationError) as excinfo:
        await list_entries(
            sessions_factory,
            StubRepositories(repo),
            50,
            "acct_merchant_payable_usd",
            far_back,
            NOW,
        )

    assert excinfo.value.details["requested_days"] == MAX_WINDOW_DAYS + 1
    assert excinfo.value.details["account_id"] == "acct_merchant_payable_usd"
    assert repo.calls == []
    assert sessions_factory.begin_count == 0


async def test_a_window_exactly_at_the_limit_is_allowed(sessions_factory) -> None:
    """92 days. A quarter, plus a day for the reporting job's off-by-one."""
    repo = StubEntryRepository()

    body = await list_entries(
        sessions_factory,
        StubRepositories(repo),
        50,
        "acct_merchant_payable_usd",
        NOW - timedelta(days=MAX_WINDOW_DAYS),
        NOW,
    )

    assert MAX_WINDOW_DAYS == 92
    assert len(body["data"]) == 3


async def test_one_open_bound_is_not_range_checked(sessions_factory) -> None:
    """`?from=` alone is "everything since", which the account filter already bounds.

    Rejecting it would break the reporting job, which pages forward from the last run.
    """
    repo = StubEntryRepository()

    await list_entries(
        sessions_factory,
        StubRepositories(repo),
        50,
        "acct_merchant_payable_usd",
        NOW - timedelta(days=400),
        None,
    )

    assert repo.calls[0]["created_from"] == NOW - timedelta(days=400)


# --------------------------------------------------------------------------------------
# append-only
# --------------------------------------------------------------------------------------


def test_the_module_exposes_one_read_route() -> None:
    """`ledger_entry` is append-only from migration `0004`. A correction is a transaction.

    Asserted structurally because the temptation is real: the ops team has asked twice for
    "just fix the amount on this entry", and the answer is a reversal plus a re-post.
    """
    methods = {method for route in entries_module.router.routes for method in route.methods}

    assert methods == {"GET"}
    assert len(entries_module.router.routes) == 1
