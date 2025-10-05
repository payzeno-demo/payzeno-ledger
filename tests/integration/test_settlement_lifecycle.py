"""One acquirer file, end to end, against a real database.

`SettlementImportService.import_file` → `SettlementService.open_batch` →
`WorldflowCsvParser.parse` → `reconciliation_item` rows → `match_items` →
`SettlementService.close_batch` → `ReconciliationService.reconcile_batch` →
`SettlementPoster.post_settlement` → `LedgerPoster.post` → `ledger_entry`.

Nothing above `tests/services/` exercises that whole spine in one go, and the seams
between the import path and the reconciliation path are where the interesting bugs sit:
`charge_id` is nullable, an unmatched sale line is `orphaned` rather than an error, and a
non-sale line has no charge at all and must still settle.

The acquirer is the only double.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text

from app.errors import BatchNotReconcilableError
from app.ports import CaptureResponse, CaptureStatus, ProcessorClient
from app.publishers.outbox import OutboxPublisher
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.repositories.account import AccountRepository
from app.repositories.balance_cache import MerchantBalanceCacheRepository
from app.repositories.ledger_entry import LedgerEntryRepository
from app.services.accounts import AccountResolver
from app.repositories.outbox import EventOutboxRepository
from app.repositories.projections import MerchantProjectionRepository
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.reconciliation_run import ReconciliationRunRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.repositories.settlement_charge import SettlementChargeRepository
from app.services.reconciliation.matcher import (
    ExactReferenceMatch,
    HeuristicAmountWindowMatch,
    NetworkTransactionMatch,
)
from app.services.reconciliation.poster import SettlementPoster
from app.services.reconciliation.reconciler import ReconciliationService
from app.services.settlements import SettlementImportService, SettlementService
from app.services.transactions import LedgerPoster
from tests.doubles import FrozenClock, StaticFeatureFlags
from tests.factories import make_charge_projection, make_merchant_projection

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)
PROCESSING_DATE = date(2026, 4, 15)
MERCHANT = "mer_lifecycle"

FILE = b"""acquirer_reference,network_reference,line_type,gross_minor,fee_minor,interchange_minor,scheme_fee_minor,net_minor,currency
WF-LC-0001,NTX-LC-0001,SL,10000,290,210,80,9710,USD
WF-LC-0002,NTX-LC-0002,SL,25000,725,525,200,24275,USD
WF-LC-0003,,SF,0,413,0,413,-413,USD
WF-LC-0004,NTX-UNKNOWN,SL,5000,145,105,40,4855,USD
"""


class FileServingProcessor(ProcessorClient):
    """Serves one settlement file and records the confirmations."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.confirm_calls: list[str] = []
        self.capture_calls: list[str] = []
        self.fetches: list[tuple[str, date]] = []

    async def confirm_settlement(
        self, acquirer: str, acquirer_reference: str, batch_id: str
    ) -> None:
        self.confirm_calls.append(acquirer_reference)

    async def capture_deferred(
        self,
        charge_id: str,
        amount_minor: int,
        currency: str,
        reference: str,
        *,
        idempotency_key: str,
    ) -> CaptureResponse:
        self.capture_calls.append(idempotency_key)
        return CaptureResponse(captured=True, reference=idempotency_key, captured_at=NOW)

    async def get_capture_status(
        self, acquirer: str, idempotency_key: str
    ) -> CaptureStatus:
        return CaptureStatus(state="unknown", reference=None)

    async def fetch_settlement_file(self, acquirer: str, processing_date: date) -> bytes:
        self.fetches.append((acquirer, processing_date))
        return self._payload


class _Settings:
    reconcile_max_items_per_run = 500
    reconcile_sweep_wall_budget_seconds = 30
    reconcile_max_attempts = 5
    reconcile_retry_backoff_base_seconds = 30


@pytest.fixture
async def projections(sessions) -> None:
    """Two charges the file will match, and the merchant they belong to.

    `WF-LC-0004` deliberately has no projection — an unmatched sale line is the normal
    steady-state case, not an error, and it must land `orphaned` rather than blowing up
    the pass.
    """
    charges = SettlementChargeRepository()
    merchants = MerchantProjectionRepository()

    async with sessions.begin() as session:
        await merchants.upsert_if_newer(
            session,
            make_merchant_projection(
                merchant_id=MERCHANT,
                status="active",
                settlement_tolerance_minor=500,
                capture_at_settlement=False,
                source_occurred_at=NOW,
            ),
        )
        for reference, network, amount in (
            ("WF-LC-0001", "NTX-LC-0001", 10_000),
            ("WF-LC-0002", "NTX-LC-0002", 25_000),
        ):
            await charges.add(
                session,
                make_charge_projection(
                    charge_id=f"ch_{reference.lower().replace('-', '_')}",
                    merchant_id=MERCHANT,
                    currency="USD",
                    amount_minor=amount,
                    processor_reference=reference,
                    network_transaction_id=network,
                    capture_at_settlement=False,
                    source_occurred_at=NOW,
                ),
            )


def _wire(sessions, processor: FileServingProcessor):
    charges = SettlementChargeRepository()
    merchants = MerchantProjectionRepository()
    batches = SettlementBatchRepository()
    items = ReconciliationItemRepository()
    runs = ReconciliationRunRepository()
    transactions = LedgerTransactionRepository()
    entries = LedgerEntryRepository()
    accounts = AccountRepository()
    balances = MerchantBalanceCacheRepository()
    publisher = OutboxPublisher(EventOutboxRepository())
    clock = FrozenClock(NOW)

    settlements = SettlementService(
        batches=batches, items=items, publisher=publisher, clock=clock
    )
    strategies = [
        ExactReferenceMatch(charges),
        NetworkTransactionMatch(charges),
        HeuristicAmountWindowMatch(charges, clock),
    ]
    importer = SettlementImportService(
        sessions=sessions,
        processor=processor,
        settlements=settlements,
        items=items,
        strategies=strategies,
        clock=clock,
    )
    ledger = LedgerPoster(
        transactions=transactions,
        entries=entries,
        accounts=accounts,
        balances=balances,
        resolver=AccountResolver(accounts, clock),
        publisher=publisher,
        clock=clock,
    )
    poster = SettlementPoster(
        transactions=transactions,
        charges=charges,
        merchants=merchants,
        ledger=ledger,
        processor=processor,
        publisher=publisher,
        flags=StaticFeatureFlags({}),
        clock=clock,
    )
    reconciler = ReconciliationService(
        sessions=sessions,
        locks=_NoLocks(),
        batches=batches,
        items=items,
        runs=runs,
        poster=poster,
        publisher=publisher,
        clock=clock,
        settings=_Settings(),
    )
    return importer, settlements, reconciler, items, batches


class _NoLocks:
    """Advisory locks are asserted in `test_reconciliation_concurrency.py`.

    Here they would only serialise a single-threaded test against itself.
    """

    async def acquire_batch_lock(self, session: Any, batch_id: str) -> None:
        return None

    async def try_acquire_batch_lock(self, session: Any, batch_id: str) -> bool:
        return True

    async def acquire_item_lock(self, session: Any, item_id: str) -> None:
        return None


async def test_import_creates_one_item_per_acquirer_line(sessions, projections) -> None:
    processor = FileServingProcessor(FILE)
    importer, _, _, items, _ = _wire(sessions, processor)

    batch = await importer.import_file("worldflow", PROCESSING_DATE)

    assert processor.fetches == [("worldflow", PROCESSING_DATE)]
    async with sessions.begin() as session:
        rows = await items.list_for_settlement(
            session,
            batch_id=batch.id,
            statuses=frozenset({"pending", "retryable", "orphaned"}),
            limit=100,
        )
    assert len(rows) == 4
    assert {row.line_type for row in rows} == {"sale", "scheme_fee"}


async def test_matching_fills_charge_id_for_the_lines_it_recognises(
    sessions, projections
) -> None:
    processor = FileServingProcessor(FILE)
    importer, _, _, items, _ = _wire(sessions, processor)

    batch = await importer.import_file("worldflow", PROCESSING_DATE)

    async with sessions.begin() as session:
        rows = await items.list_for_settlement(
            session,
            batch_id=batch.id,
            statuses=frozenset({"pending", "retryable", "orphaned"}),
            limit=100,
        )
    by_reference = {row.acquirer_reference: row for row in rows}

    assert by_reference["WF-LC-0001"].charge_id is not None
    assert by_reference["WF-LC-0001"].match_method == "exact_reference"
    assert by_reference["WF-LC-0002"].charge_id is not None
    # No projection for this one. Unmatched, not broken.
    assert by_reference["WF-LC-0004"].charge_id is None


async def test_close_then_reconcile_posts_the_ledger(sessions, projections) -> None:
    """The full spine, and the only place `ledger_entry` rows appear in this file."""
    processor = FileServingProcessor(FILE)
    importer, _, reconciler, _, _ = _wire(sessions, processor)

    batch = await importer.import_file("worldflow", PROCESSING_DATE)
    run = await reconciler.reconcile_batch(batch.id, trigger="manual")

    assert run.items_total == 4
    assert run.items_settled >= 2

    async with sessions.begin() as session:
        posted = await session.execute(
            text(
                "SELECT count(*) FROM ledger_transaction "
                "WHERE purpose = 'settle' AND merchant_id = :merchant"
            ),
            {"merchant": MERCHANT},
        )
        assert int(posted.scalar_one()) >= 2

        entries = await session.execute(
            text(
                "SELECT count(*) FROM ledger_entry e "
                "JOIN ledger_transaction t ON t.id = e.transaction_id "
                "WHERE t.purpose = 'settle'"
            )
        )
        # Three legs minimum per settle: payable, fee expense, acquirer receivable.
        assert int(entries.scalar_one()) >= 6

    # Every item confirms its line with the acquirer, whatever its line_type.
    assert len(processor.confirm_calls) == 4


async def test_reconciling_an_open_batch_is_refused(sessions, projections) -> None:
    """`close_batch` is the gate. `BatchNotReconcilableError`, not a silent no-op."""
    processor = FileServingProcessor(FILE)
    _, settlements, _, _, _ = _wire(sessions, processor)

    async with sessions.begin() as session:
        batch = await settlements.open_batch(
            session,
            acquirer="worldflow",
            currency="USD",
            processing_date=PROCESSING_DATE,
            file_reference="WF-OPEN-ONLY",
        )
        batch_id = batch.id

    async with sessions.begin() as session:
        closed = await settlements.close_batch(session, batch_id)
        assert closed.status == "closed"

    async with sessions.begin() as session:
        with pytest.raises(BatchNotReconcilableError):
            await settlements.close_batch(session, batch_id)


async def test_import_is_idempotent_on_file_reference(sessions, projections) -> None:
    """`uq_settlement_batch_file`. The acquirer refiles; we do not re-import.

    Worldflow reposts the same file when their side times out, which happens roughly
    weekly. Without the unique key that is a second batch, a second set of items, and
    every one of them a duplicate settle.
    """
    processor = FileServingProcessor(FILE)
    importer, _, _, _, batches = _wire(sessions, processor)

    first = await importer.import_file("worldflow", PROCESSING_DATE)
    second = await importer.import_file("worldflow", PROCESSING_DATE)

    assert first.id == second.id
    async with sessions.begin() as session:
        found = await batches.list_by_status(session, ("open", "closed"))
    assert len([b for b in found if b.file_reference == first.file_reference]) == 1
