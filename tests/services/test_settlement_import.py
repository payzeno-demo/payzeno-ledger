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

    async def open_batch(
        self,
        session: Any,
        *,
        acquirer: str,
        currency: str,
        processing_date: date,
        file_reference: str,
        livemode: bool = True,
    ) -> Any:
        if file_reference in self.by_file:
            return self.batches[self.by_file[file_reference]]
        self._seq += 1
        batch = make_batch(
            batch_id=f"sb_imp_{self._seq}",
            acquirer=acquirer,
            currency=currency,
            status="open",
            processing_date=processing_date,
            file_reference=file_reference,
        )
        self.batches[batch.id] = batch
        self.by_file[file_reference] = batch.id
        return batch

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

    async def add_all(self, session: Any, objs: list[Any]) -> list[Any]:
        self.added.extend(objs)
        return objs

    async def add(self, session: Any, obj: Any) -> Any:
        self.added.append(obj)
        return obj


class NullChargeRepository:
    async def find_by_processor_reference(
        self, session: Any, *, acquirer: str, processor_reference: str
    ) -> Any | None:
        return None


def _importer(sessions_factory, processor: StubProcessor):
    settlements = RecordingSettlementService()
    items = CollectingItemRepository()
    service = SettlementImportService(
        sessions=sessions_factory,
        processor=processor,
        settlements=settlements,
        items=items,
        strategies=[ExactReferenceMatch(NullChargeRepository())],
        clock=FrozenClock(NOW),
    )
    return service, settlements, items


# --------------------------------------------------------------------------------------
# import_file
# --------------------------------------------------------------------------------------


async def test_import_file_fetches_from_the_acquirer(sessions_factory) -> None:
    processor = StubProcessor()
    service, _, _ = _importer(sessions_factory, processor)

    await service.import_file("worldflow", PROCESSING_DATE)

    assert processor.fetches == [("worldflow", PROCESSING_DATE)]


async def test_import_file_creates_one_item_per_line(sessions_factory) -> None:
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

    by_reference = {row.acquirer_reference: row for row in items.added}
    assert by_reference["WF-2001"].line_type == "sale"
    assert by_reference["WF-2003"].line_type == "scheme_fee"


async def test_import_file_closes_the_batch(sessions_factory) -> None:
    """The sweep only looks at `closed` and `partially_reconciled` batches.

    An import that forgets this step leaves the items sitting there forever, and the only
    symptom is a merchant asking why they have not been paid.
    """
    processor = StubProcessor()
    service, settlements, _ = _importer(sessions_factory, processor)

    batch = await service.import_file("worldflow", PROCESSING_DATE)

    assert settlements.closed == [batch.id]
    assert batch.status == "closed"


async def test_import_file_is_a_no_op_for_a_file_already_imported(sessions_factory) -> None:
    """Worldflow reposts on its own timeouts, roughly weekly."""
    processor = StubProcessor()
    service, settlements, _ = _importer(sessions_factory, processor)

    first = await service.import_file("worldflow", PROCESSING_DATE)
    second = await service.import_file("worldflow", PROCESSING_DATE)

    assert first.id == second.id
    assert len(settlements.batches) == 1


async def test_import_file_propagates_an_acquirer_outage(sessions_factory) -> None:
    processor = StubProcessor(raises=ProcessorUnavailableError(code="processor_unavailable"))
    service, settlements, items = _importer(sessions_factory, processor)

    with pytest.raises(ProcessorUnavailableError):
        await service.import_file("worldflow", PROCESSING_DATE)

    assert settlements.batches == {}
    assert items.added == []


async def test_import_file_rejects_a_file_with_no_header(sessions_factory) -> None:
    processor = StubProcessor(payload=b"")
    service, _, items = _importer(sessions_factory, processor)

    with pytest.raises(ValidationError):
        await service.import_file("worldflow", PROCESSING_DATE)

    assert items.added == []


# --------------------------------------------------------------------------------------
# parser selection — two formats, one interface, no plan to converge
# --------------------------------------------------------------------------------------


async def test_parser_for_picks_the_acquirer_format() -> None:
    assert isinstance(parser_for("worldflow"), WorldflowCsvParser)
    assert isinstance(parser_for("nordpay"), LegacyFixedWidthParser)


async def test_parser_for_rejects_an_unknown_acquirer() -> None:
    with pytest.raises(ValidationError):
        parser_for("stripe")


async def test_worldflow_parser_reads_columns_by_name_not_position() -> None:
    """Worldflow reorders columns between file versions without telling anyone."""
    reordered = (
        b"currency,net_minor,line_type,acquirer_reference,gross_minor,fee_minor,"
        b"interchange_minor,scheme_fee_minor\n"
        b"USD,9710,SL,WF-3001,10000,290,210,80\n"
    )

    lines = WorldflowCsvParser().parse(reordered)

    assert len(lines) == 1
    assert lines[0].acquirer_reference == "WF-3001"
    assert lines[0].gross_minor == 10_000
    assert lines[0].net_minor == 9_710


# --------------------------------------------------------------------------------------
# the legacy push path — POST /internal/v1/settlement-imports
# --------------------------------------------------------------------------------------


async def test_import_legacy_records_opens_and_returns_a_count() -> None:
    """The route payzeno-billing-legacy's export job pushes to.

    It predates `SettlementImportService` and duplicates its matching. Nobody has time to
    converge them, so it gets its own test instead.
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

    async def add(self, session: Any, obj: Any) -> Any:
        self.rows[obj.id] = obj
        self.by_file[obj.file_reference] = obj.id
        return obj

    async def find_by_file_reference(
        self, session: Any, *, acquirer: str, file_reference: str
    ) -> Any | None:
        batch_id = self.by_file.get(file_reference)
        return self.rows.get(batch_id) if batch_id else None

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        return self.rows[entity_id]

    async def mark_status(self, session: Any, batch_id: str, *, status: str) -> Any:
        self.rows[batch_id].status = status
        return self.rows[batch_id]
