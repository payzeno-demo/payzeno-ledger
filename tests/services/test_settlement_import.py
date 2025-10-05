"""`SettlementImportService` and `SettlementService` — app/services/settlements.py.

This is the input to the entire reconciliation subsystem. Nothing else opens a batch and
nothing else creates a `reconciliation_item`, so a bug here is invisible until a
settlement quietly never happens.

`import_file` is a five-step pipeline and each step has its own failure mode:

1. `ProcessorClient.fetch_settlement_file` — the acquirer can 502
2. `open_batch` — `uq_settlement_batch_file` makes a refile a no-op
3. parse — a malformed file must not create a half-imported batch
4. `match_items` — an unmatched sale line is normal, not an error
5. `close_batch` — the gate the sweep looks for

The legacy path (`import_legacy_records`) is tested here too. It predates this service,
duplicates about forty lines of its matching, and the Java side still pushes to it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.errors import (
    BatchNotReconcilableError,
    ProcessorUnavailableError,
    ValidationError,
)
from app.services.reconciliation.matcher import ExactReferenceMatch
from app.services.settlement_parser import (
    LegacyFixedWidthParser,
    WorldflowCsvParser,
    parser_for,
)
from app.services.settlements import SettlementImportService, SettlementService
from tests.doubles import CollectingPublisher, FrozenClock
from tests.factories import make_batch

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)
PROCESSING_DATE = date(2026, 4, 15)

CSV_FILE = b"""acquirer_reference,network_reference,line_type,gross_minor,fee_minor,interchange_minor,scheme_fee_minor,net_minor,currency
WF-2001,NTX-2001,SL,10000,290,210,80,9710,USD
WF-2002,NTX-2002,SL,25000,725,525,200,24275,USD
WF-2003,,SF,0,413,0,413,-413,USD
"""


class StubProcessor:
    def __init__(self, payload: bytes | None = None, raises: Exception | None = None) -> None:
        self.payload = payload if payload is not None else CSV_FILE
        self.raises = raises
        self.fetches: list[tuple[str, date]] = []

    async def fetch_settlement_file(self, acquirer: str, processing_date: date) -> bytes:
        self.fetches.append((acquirer, processing_date))
        if self.raises is not None:
            raise self.raises
        return self.payload


class RecordingSettlementService(SettlementService):
    """The real class's surface with in-memory batches.

    Subclassed rather than mocked so `import_file` still calls `open_batch` and
    `close_batch` by name and still gets `BatchNotReconcilableError` out of the real
    guard when the state machine is violated.
    """

    def __init__(self) -> None:
        self.batches: dict[str, Any] = {}
        self.by_file: dict[str, str] = {}
        self.closed: list[str] = []
        self._seq = 0

    async def close_batch(self, session: Any, batch_id: str) -> Any:
        batch = self.batches[batch_id]
        if batch.status != "open":
            raise BatchNotReconcilableError(
                f"batch {batch_id} is {batch.status}", batch_id=batch_id
            )
        batch.status = "closed"
        self.closed.append(batch_id)
        return batch


class CollectingItemRepository:
    def __init__(self) -> None:
        self.added: list[Any] = []

    async def add(self, session: Any, obj: Any) -> Any:
        self.added.append(obj)
        return obj


class NullChargeRepository:
    settlements = RecordingSettlementService()
    items = CollectingItemRepository()
    processor = StubProcessor()
    service, _, items = _importer(sessions_factory, processor)

    await service.import_file("worldflow", PROCESSING_DATE)

    assert len(items.added) == 3
    assert {row.acquirer_reference for row in items.added} == {
        "WF-2001",
        "WF-2002",
        "WF-2003",
    }


async def test_import_file_carries_line_type_through_from_the_file(sessions_factory) -> None:
    """`line_type` picks the `PostingRule`. A wrong code books the wrong legs."""
    processor = StubProcessor()
    service, _, items = _importer(sessions_factory, processor)

    await service.import_file("worldflow", PROCESSING_DATE)

    first = await service.import_file("worldflow", PROCESSING_DATE)
    second = await service.import_file("worldflow", PROCESSING_DATE)

    assert first.id == second.id
    assert len(settlements.batches) == 1


async def test_import_file_propagates_an_acquirer_outage(sessions_factory) -> None:
    """
    service = SettlementService(
        batches=_InMemoryBatches(),
        items=CollectingItemRepository(),
        publisher=CollectingPublisher(),
        clock=FrozenClock(NOW),
    )

    batch_id, count = await service.import_legacy_records(
        object(),
        acquirer="nordpay",
        processing_date=PROCESSING_DATE,
        file_reference="LEGACY-20260415",
        records=[
            {
                "acquirer_reference": "NP-9001",
                "line_type": "sale",
                "gross_minor": 4_000,
                "fee_minor": 116,
                "net_minor": 3_884,
                "currency": "EUR",
            }
        ],
        strategies=[ExactReferenceMatch(NullChargeRepository())],
    )

    assert batch_id
    assert count == 1


class _InMemoryBatches:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.by_file: dict[str, str] = {}
        self._seq = 0

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        return self.rows[entity_id]

    async def mark_status(self, session: Any, batch_id: str, *, status: str) -> Any:
        self.rows[batch_id].status = status
        return self.rows[batch_id]
