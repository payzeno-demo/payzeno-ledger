"""`app/api/routers/settlements.py` — the batch half of api-surface.md §10.3.

Six routes. Two of them — `open_batch` and `close_batch` — have exactly one caller each,
`SettlementImportService.import_file`, and they are exposed over HTTP anyway so that an
operator can drive a manual import when an acquirer files late. That is the kind of thing
that reads as over-engineering until 23:00 on a Friday.

`list_items` takes `merchant_id` as a **required** query parameter rather than an optional
filter. A batch holds every merchant's lines for that acquirer and day, so an unscoped
listing hands payzeno-api another merchant's settlement detail, and payzeno-api would
forward it. That requirement is a tenancy control, not a performance one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.api.routers.settlements import (
    close_batch,
    get_batch,
    import_legacy_records,
    list_batches,
    list_items,
    open_batch,
    record_funding,
)
from app.errors import BatchNotFoundError, BatchNotReconcilableError, ValidationError
from app.repositories.base import Page
from tests.factories import make_batch, make_item

pytestmark = pytest.mark.asyncio

#: `require_internal_service` resolved these. The legacy push route sees the other one.
CALLER = "payzeno-api"
LEGACY_CALLER = "payzeno-billing-legacy"

NOW = datetime(2026, 4, 16, 16, 0, tzinfo=UTC)
PROCESSING_DATE = date(2026, 4, 15)


class StubSettlements:
    def __init__(self, *, closed_already: bool = False) -> None:
        self.opened: list[dict[str, Any]] = []
        self.closed: list[str] = []
        self.closed_already = closed_already
        self.legacy: list[dict[str, Any]] = []

    async def open_batch(self, session: Any, **kwargs: Any) -> Any:
        self.opened.append(kwargs)
        return make_batch(
            batch_id="sb_new",
            acquirer=kwargs["acquirer"],
            currency=kwargs["currency"],
            status="open",
            processing_date=kwargs["processing_date"],
            file_reference=kwargs["file_reference"],
        )

    async def close_batch(self, session: Any, batch_id: str) -> Any:
        if self.closed_already:
            raise BatchNotReconcilableError(f"batch {batch_id} is not open", batch_id=batch_id)
        self.closed.append(batch_id)
        return make_batch(batch_id=batch_id, status="closed")

    async def import_legacy_records(self, session: Any, **kwargs: Any) -> tuple[str, int]:
        self.legacy.append(kwargs)
        return "sb_legacy", len(kwargs["records"])


class StubBatches:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = rows or {}
        self.filters: list[dict[str, Any]] = []

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        try:
            return self.rows[entity_id]
        except KeyError:
            raise BatchNotFoundError(entity_id=entity_id) from None

    async def list_page(self, session: Any, *, cursor: str | None, limit: int, **filters: Any) -> Page:
        self.filters.append(filters)
        return Page(items=list(self.rows.values()), next_cursor=None, has_more=False)


class StubItems:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.filters: list[dict[str, Any]] = []

    async def list_page(self, session: Any, *, cursor: str | None, limit: int, **filters: Any) -> Page:
        self.filters.append(filters)
        rows = [
            row
            for row in self.rows
            if filters.get("merchant_id") in (None, row.merchant_id)
        ]
        return Page(items=rows, next_cursor=None, has_more=False)


class StubFunding:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_funding(self, session: Any, **kwargs: Any) -> Any:
        self.recorded.append(kwargs)
        return make_batch(batch_id=kwargs["batch_id"], status="funded")


class Record:
    """One line of a legacy push. `import_legacy_records` calls `model_dump()` on each."""

    def __init__(self, **fields: Any) -> None:
        self.fields = fields

    def model_dump(self) -> dict[str, Any]:
        return dict(self.fields)


class StubRepositories:
    """The container's repository namespace, plus the match strategies the legacy import
    hands to `SettlementService`.

    Three attributes, which is exactly what this router reaches for. Anything else it
    started using would fail here rather than in staging.
    """

    def __init__(
        self,
        *,
        batches: "StubBatches | None" = None,
        items: "StubItems | None" = None,
    ) -> None:
        self.settlement_batches = batches or StubBatches()
        self.reconciliation_items = items or StubItems()
        self.match_strategies = ["exact_reference", "network_transaction", "amount_window"]


def _body(**kwargs: Any) -> Any:
    return type("Body", (), kwargs)()


# --------------------------------------------------------------------------------------
# batches
# --------------------------------------------------------------------------------------


async def test_open_batch_creates_it(sessions_factory) -> None:
    settlements = StubSettlements()

    batch = await open_batch(
        _body(
            acquirer="worldflow",
            currency="USD",
            processing_date=PROCESSING_DATE,
            file_reference="WF-20260415",
        ),
        sessions_factory,
        settlements,
        CALLER,
    )

    assert batch["status"] == "open"
    assert settlements.opened[0]["file_reference"] == "WF-20260415"


async def test_close_batch_returns_the_closed_batch(sessions_factory) -> None:
    settlements = StubSettlements()

    batch = await close_batch(sessions_factory, settlements, CALLER, "sb_1")

    assert batch["status"] == "closed"
    assert settlements.closed == ["sb_1"]


async def test_closing_a_batch_that_is_not_open_is_422(sessions_factory) -> None:
    """`batch_not_reconcilable`. The state machine is the guard, not the caller."""
    settlements = StubSettlements(closed_already=True)

    with pytest.raises(BatchNotReconcilableError) as excinfo:
        await close_batch(sessions_factory, settlements, CALLER, "sb_1")

    assert excinfo.value.http_status == 422


async def test_get_batch_returns_it(sessions_factory) -> None:
    batches = StubBatches({"sb_1": make_batch(batch_id="sb_1", status="closed")})

    batch = await get_batch(
        sessions_factory, StubRepositories(batches=batches), "sb_1", None
    )

    assert batch["id"] == "sb_1"


async def test_getting_an_unknown_batch_is_a_404(sessions_factory) -> None:
    batches = StubBatches()

    with pytest.raises(BatchNotFoundError) as excinfo:
        await get_batch(
            sessions_factory, StubRepositories(batches=batches), "sb_ghost", None
        )

    assert excinfo.value.http_status == 404


async def test_listing_batches_passes_its_filters(sessions_factory) -> None:
    batches = StubBatches({"sb_1": make_batch(batch_id="sb_1", status="closed")})

    page = await list_batches(
        sessions_factory,
        StubRepositories(batches=batches),
        50,
        "mer_api",
        "USD",
        "closed",
        "worldflow",
        None,
    )

    assert page["has_more"] is False
    assert batches.filters[0]["status"] == "closed"
    assert batches.filters[0]["acquirer"] == "worldflow"


# --------------------------------------------------------------------------------------
# items — the tenancy control
# --------------------------------------------------------------------------------------


async def test_listing_items_requires_a_merchant(sessions_factory) -> None:
    """A batch holds every merchant's lines for that acquirer and day.

    Unscoped, this hands payzeno-api another merchant's settlement detail, and
    payzeno-api forwards whatever the ledger returns.
    """
    items = StubItems()

    with pytest.raises(ValidationError):
        await list_items(
            sessions_factory, StubRepositories(items=items), 50, "sb_1", None
        )


async def test_listing_items_scopes_to_the_merchant(sessions_factory) -> None:
    items = StubItems(
        [
            make_item(item_id="ri_1", batch_id="sb_1", merchant_id="mer_a"),
            make_item(item_id="ri_2", batch_id="sb_1", merchant_id="mer_b"),
        ]
    )

    page = await list_items(
        sessions_factory, StubRepositories(items=items), 50, "sb_1", "mer_a"
    )

    assert [row["id"] for row in page["data"]] == ["ri_1"]


async def test_listing_items_filters_by_status_and_line_type(sessions_factory) -> None:
    items = StubItems([make_item(item_id="ri_1", batch_id="sb_1", merchant_id="mer_a")])

    await list_items(
        sessions_factory,
        StubRepositories(items=items),
        50,
        "sb_1",
        "mer_a",
        "retryable",
        "sale",
        None,
    )

    assert items.filters[0]["status"] == "retryable"
    assert items.filters[0]["line_type"] == "sale"


# --------------------------------------------------------------------------------------
# funding
# --------------------------------------------------------------------------------------


async def test_recording_funding_marks_the_batch_funded(sessions_factory) -> None:
    """Until a batch is funded its credits do not count toward `compute_available`.

    This route is how the treasury tooling tells the ledger the money actually landed.
    """
    funding = StubFunding()

    batch = await record_funding(
        _body(bank_reference="WIRE-99812", amount_minor=1_982_000, value_date=PROCESSING_DATE),
        sessions_factory,
        funding,
        CALLER,
        "sb_1",
    )

    assert batch["status"] == "funded"
    assert funding.recorded[0]["bank_reference"] == "WIRE-99812"


# --------------------------------------------------------------------------------------
# §10.6 — the route payzeno-billing-legacy pushes to
# --------------------------------------------------------------------------------------


async def test_the_legacy_import_route_reports_what_it_staged(sessions_factory) -> None:
    """`LedgerReconciliationExportJob` on the Java side calls this.

    It predates `SettlementImportService` and duplicates about forty lines of its
    matching. Converging them is arc MIG work that nobody has scheduled.
    """
    settlements = StubSettlements()

    response = await import_legacy_records(
        _body(
            acquirer="nordpay",
            processing_date=PROCESSING_DATE,
            file_reference="LEGACY-20260415",
            records=[Record(acquirer_reference="NP-1"), Record(acquirer_reference="NP-2")],
        ),
        sessions_factory,
        settlements,
        StubRepositories(),
        LEGACY_CALLER,
    )

    assert response["batch_id"] == "sb_legacy"
    assert response["item_count"] == 2
