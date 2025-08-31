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

    async def list_page(self, session: Any, *, cursor: str | None, limit: int, **filters: Any) -> Page:
        self.filters.append(filters)
        return Page(items=list(self.rows.values()), next_cursor=None, has_more=False)


class StubItems:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def __init__(self, **fields: Any) -> None:
        self.fields = fields

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

    """`batch_not_reconcilable`. The state machine is the guard, not the caller."""
    settlements = StubSettlements(closed_already=True)

    with pytest.raises(BatchNotReconcilableError) as excinfo:
        await close_batch(sessions_factory, settlements, CALLER, "sb_1")

    assert excinfo.value.http_status == 422


async def test_get_batch_returns_it(sessions_factory) -> None:
    batches = StubBatches({"sb_1": make_batch(batch_id="sb_1", status="closed")})

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
