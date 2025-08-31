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

    async def add(self, session: Any, obj: Any) -> Any:
        self.added.append(obj)
        return obj


class NullChargeRepository:
    async def mark_status(self, session: Any, batch_id: str, *, status: str) -> Any:
        self.rows[batch_id].status = status
        return self.rows[batch_id]
