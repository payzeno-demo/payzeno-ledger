"""Settlement batch lifecycle and file import.

Nothing else in payzeno-ledger opens a batch or creates a ``reconciliation_item``. The
whole reconciliation subsystem is downstream of this module, which is why an import that
half-runs is worse than one that does not run at all: every item this file fails to
create is money the ledger will never notice is missing.

Two entry points, and they are not equivalent:

* :class:`SettlementImportService` — the modern path. Pulls the file from the acquirer,
  parses it, opens the batch, matches, closes. Since RFC platform-v2 §7 it asks for one
  currency at a time (``?date=&currency=``) and opens one batch per acquirer, date and
  currency, ``file_reference = "{acquirer}-{date}-{currency}"``.
* :meth:`SettlementService.import_legacy_records` — the ``POST /internal/v1/settlement-imports``
  route payzeno-billing-legacy's ``LedgerReconciliationExportJob`` pushes to. It predates
  the import service and duplicates a chunk of its matching, because when it was written
  there was nothing to reuse.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ids import new_id
from app.errors import BatchNotFoundError, BatchNotReconcilableError, ValidationError
from app.logging import get_logger
from app.metrics import metrics
from app.models.reconciliation_item import ReconciliationItem
from app.models.settlement_batch import SettlementBatch
from app.ports import Clock, EventPublisher, ProcessorClient, SessionFactory
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.services.reconciliation.matcher import MatchStrategy, match_items
from app.services.settlement_parser import ParsedSettlementLine, parser_for

logger = get_logger(__name__)

#: How many days either side of a file's processing date the importer looks for a line
#: some other file from the same acquirer already carried. A week covers an acquirer
#: re-filing a line a few days late and the switch from one mixed file a day to one file
#: per currency, which is when the same line can arrive under two references.
DUPLICATE_LINE_WINDOW_DAYS: Final[int] = 7


def file_reference_for(acquirer: str, processing_date: date, currency: str | None = None) -> str:
    """The batch's ``file_reference``: per currency since RFC platform-v2, per day before."""
    base = f"{acquirer}-{processing_date.isoformat()}"
    return base if currency is None else f"{base}-{currency}"


class SettlementService:
    """Opens, closes and funds settlement batches."""

    def __init__(
        self,
        batches: SettlementBatchRepository,
        items: ReconciliationItemRepository,
        publisher: EventPublisher,
        clock: Clock,
    ) -> None:
        self._batches = batches
        self._items = items
        self._publisher = publisher
        self._clock = clock

    async def open_batch(
        self,
        session: AsyncSession,
        *,
        acquirer: str,
        currency: str,
        processing_date: date,
        file_reference: str,
        livemode: bool = True,
    ) -> SettlementBatch:
        """Create the batch, or return the existing one for this file.

        ``uq_settlement_batch_file (acquirer, file_reference)`` makes re-importing the
        same acquirer file a no-op rather than a duplicate batch.
        """
        existing = await self._batches.find_by_file(
            session, acquirer=acquirer, file_reference=file_reference
        )
        if existing is not None:
            logger.info(
                "settlement_batch_reused",
                batch_id=existing.id,
                acquirer=acquirer,
                file_reference=file_reference,
            )
            return existing

        batch = SettlementBatch(
            id=new_id("sb"),
            acquirer=acquirer,
            currency=currency,
            processing_date=processing_date,
            file_reference=file_reference,
            status="open",
            livemode=livemode,
            opened_at=self._clock.now(),
        )
        await self._batches.add(session, batch)
        logger.info("settlement_batch_opened", batch_id=batch.id, acquirer=acquirer)
        return batch

    async def find_imported_batch(
        self,
        session: AsyncSession,
        *,
        acquirer: str,
        processing_date: date,
        currency: str,
    ) -> SettlementBatch | None:
        """The batch an earlier import opened for this acquirer, date and currency.

        Looks under the per-currency reference first and then under the per-day reference
        files had before the acquirers split them by currency — a day imported before the
        switch is imported, and fetching it again per currency would open a second batch
        of the same money.
        """
        for reference in (
            file_reference_for(acquirer, processing_date, currency),
            file_reference_for(acquirer, processing_date),
        ):
            batch = await self._batches.find_by_file(
                session, acquirer=acquirer, file_reference=reference
            )
            if batch is not None and batch.currency == currency:
                return batch
        return None

    async def latest_processing_date(self, session: AsyncSession, acquirer: str) -> date | None:
        """The newest processing date this acquirer has a batch for, if any."""
        return await self._batches.latest_processing_date(session, acquirer=acquirer)

    async def close_batch(self, session: AsyncSession, batch_id: str) -> SettlementBatch:
        """Close an ``open`` batch and emit ``settlement.batch_closed``."""
        batch = await self._batches.get(session, batch_id)
        if batch is None:
            raise BatchNotFoundError(f"settlement batch {batch_id} not found", batch_id=batch_id)
        if batch.status != "open":
            raise BatchNotReconcilableError(
                f"batch {batch_id} is {batch.status}, not open",
                batch_id=batch_id,
                status=batch.status,
            )

        totals = await self._items.totals_for_batch(session, batch_id)
        batch.item_count = totals.item_count
        batch.expected_total_minor = totals.net_minor
        batch.status = "closed"
        batch.closed_at = self._clock.now()

        await self._publisher.publish(
            "settlement.batch_closed",
            {
                "batch_id": batch.id,
                "acquirer": batch.acquirer,
                "currency": batch.currency,
                "processing_date": batch.processing_date.isoformat(),
                "item_count": batch.item_count,
                "expected_total_minor": batch.expected_total_minor,
                "closed_at": batch.closed_at.isoformat(),
            },
            merchant_id=None,
            correlation_id=batch.id,
            session=session,
            livemode=batch.livemode,
        )
        logger.info(
            "settlement_batch_closed",
            batch_id=batch.id,
            item_count=batch.item_count,
            expected_total_minor=batch.expected_total_minor,
        )
        return batch

    async def import_legacy_records(
        self,
        session: AsyncSession,
        *,
        acquirer: str,
        processing_date: date,
        file_reference: str,
        records: Sequence[dict[str, Any]],
        strategies: list[MatchStrategy],
    ) -> tuple[str, int]:
        """The Java service's push path. Predates :class:`SettlementImportService`.

        ``LegacySettlementRecord`` arrives already parsed on the Java side, so this does
        not go through a parser at all — it builds items straight from the payload and
        then runs the same matcher walk the import service runs. The duplication is
        known and is the reason arc MIG lists this route for deletion.
        """
        if not records:
            raise ValidationError("settlement import carried no records", acquirer=acquirer)

        currency = str(records[0].get("currency", "USD")).upper()
        batch = await self.open_batch(
            session,
            acquirer=acquirer,
            currency=currency,
            processing_date=processing_date,
            file_reference=file_reference,
        )
        if batch.status != "open":
            # A re-push of a file whose batch has already closed. Nothing to add.
            logger.info(
                "legacy_settlement_import_skipped",
                batch_id=batch.id,
                status=batch.status,
                file_reference=file_reference,
            )
            return batch.id, 0
        existing = await self._items.acquirer_references_for_batch(session, batch.id)

        items: list[ReconciliationItem] = []
        for record in records:
            if str(record["acquirer_reference"]) in existing:
                continue
            items.append(
                ReconciliationItem(
                    id=new_id("ri"),
                    batch_id=batch.id,
                    charge_id=None,
                    merchant_id=record.get("merchant_id"),
                    line_type=str(record.get("line_type", "sale")),
                    gross_minor=int(record.get("gross_minor", 0)),
                    fee_minor=int(record.get("fee_minor", 0)),
                    interchange_minor=int(record.get("interchange_minor", 0)),
                    scheme_fee_minor=int(record.get("scheme_fee_minor", 0)),
                    net_minor=int(record.get("net_minor", 0)),
                    currency=str(record.get("currency", currency)).upper(),
                    livemode=batch.livemode,
                    acquirer=batch.acquirer,
                    acquirer_reference=str(record["acquirer_reference"]),
                    network_reference=record.get("network_reference"),
                    status="pending",
                    match_method="unmatched",
                    next_attempt_at=self._clock.now(),
                )
            )
        await self._items.add_all(session, items)
        await match_items(session, items, strategies, clock=self._clock)

        metrics.increment("LegacySettlementImport", acquirer=acquirer)
        logger.info(
            "legacy_settlement_import",
            batch_id=batch.id,
            acquirer=acquirer,
            item_count=len(items),
        )
        return batch.id, len(items)


class SettlementImportService:
    """Pulls one acquirer settlement file and turns it into a closed, matched batch.

    Sole caller of ``ProcessorClient.fetch_settlement_file``, of
    :meth:`SettlementService.open_batch` and of :meth:`SettlementService.close_batch`.
    Driven by ``SettlementImportJob`` (3600s).
    """

    def __init__(
        self,
        sessions: SessionFactory,
        processor: ProcessorClient,
        settlements: SettlementService,
        items: ReconciliationItemRepository,
        strategies: list[MatchStrategy],
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._processor = processor
        self._settlements = settlements
        self._items = items
        self._strategies = strategies
        self._clock = clock

    async def latest_processing_date(self, acquirer: str) -> date | None:
        """Where ``SettlementImportJob`` resumes its walk after the clock jumped forward."""
        async with self._sessions.begin() as session:
            return await self._settlements.latest_processing_date(session, acquirer)

    async def import_file(
        self, acquirer: str, processing_date: date, currency: str | None = None
    ) -> SettlementBatch | None:
        """Import one acquirer file; with ``currency``, that currency's file only.

        Returns the batch — the existing one when the file was imported before — or
        ``None`` when the per-currency file has no lines for the currency, which is the
        normal answer for most currencies on most days and opens no batch.

        Raises :class:`~app.errors.SettlementFileNotReadyError` when the acquirer has not
        published the file yet, and the acquirer's own errors otherwise.
        """
        if currency is not None:
            async with self._sessions.begin() as session:
                done = await self._settlements.find_imported_batch(
                    session,
                    acquirer=acquirer,
                    processing_date=processing_date,
                    currency=currency,
                )
            if done is not None and done.status != "open":
                # The hourly job re-walks its window, so most calls land here — and
                # without a round trip to the acquirer.
                return done

        raw = await self._processor.fetch_settlement_file(
            acquirer=acquirer, processing_date=processing_date, currency=currency
        )
        parsed = parser_for(acquirer).parse(raw)
        if currency is not None:
            foreign = sum(1 for line in parsed if line.currency != currency)
            if foreign:
                # An acquirer that ignored ?currency= sent the whole day. The other
                # currencies' lines are not lost: each is picked up by its own request.
                logger.info(
                    "settlement_file_other_currency_lines",
                    acquirer=acquirer,
                    processing_date=processing_date.isoformat(),
                    currency=currency,
                    skipped=foreign,
                )
            parsed = [line for line in parsed if line.currency == currency]
        if not parsed:
            logger.info(
                "settlement_file_empty",
                acquirer=acquirer,
                processing_date=processing_date.isoformat(),
                currency=currency,
            )
            if currency is not None:
                return None

        file_reference = file_reference_for(acquirer, processing_date, currency)
        batch_currency = currency or (parsed[0].currency if parsed else "USD")

        async with self._sessions.begin() as session:
            batch = await self._settlements.open_batch(
                session,
                acquirer=acquirer,
                currency=batch_currency,
                processing_date=processing_date,
                file_reference=file_reference,
            )
            if batch.status != "open":
                logger.info(
                    "settlement_file_already_imported",
                    batch_id=batch.id,
                    acquirer=acquirer,
                    status=batch.status,
                )
                return batch
            # An open batch with items is an import that stopped before closing. Add only
            # the lines it does not have yet, then close it.
            existing = await self._items.acquirer_references_for_batch(session, batch.id)
            new_lines = [line for line in parsed if line.acquirer_reference not in existing]
            if currency is not None and new_lines:
                filed = await self._items.acquirer_references_already_filed(
                    session,
                    acquirer=acquirer,
                    currency=currency,
                    around=processing_date,
                    window_days=DUPLICATE_LINE_WINDOW_DAYS,
                    references=[line.acquirer_reference for line in new_lines],
                )
                if filed:
                    logger.warning(
                        "settlement_lines_already_filed",
                        batch_id=batch.id,
                        acquirer=acquirer,
                        currency=currency,
                        skipped=len(filed),
                    )
                    metrics.increment("SettlementLineAlreadyFiled", acquirer=acquirer)
                    new_lines = [line for line in new_lines if line.acquirer_reference not in filed]
            items = self._build_items(batch, new_lines)
            await self._items.add_all(session, items)
            await match_items(session, items, self._strategies, clock=self._clock)
            batch_id = batch.id

        async with self._sessions.begin() as session:
            batch = await self._settlements.close_batch(session, batch_id)

        metrics.increment("SettlementFileImported", acquirer=acquirer)
        logger.info(
            "settlement_file_imported",
            batch_id=batch_id,
            acquirer=acquirer,
            currency=batch_currency,
            item_count=len(parsed),
        )
        return batch

    def _build_items(
        self, batch: SettlementBatch, parsed: list[ParsedSettlementLine]
    ) -> list[ReconciliationItem]:
        now = self._clock.now()
        return [
            ReconciliationItem(
                id=new_id("ri"),
                batch_id=batch.id,
                charge_id=None,
                merchant_id=None,
                line_type=line.line_type,
                gross_minor=line.gross_minor,
                fee_minor=line.fee_minor,
                interchange_minor=line.interchange_minor,
                scheme_fee_minor=line.scheme_fee_minor,
                net_minor=line.net_minor,
                currency=line.currency,
                livemode=batch.livemode,
                acquirer=batch.acquirer,
                acquirer_reference=line.acquirer_reference,
                network_reference=line.network_reference,
                status="pending",
                match_method="unmatched",
                next_attempt_at=now,
            )
            for line in parsed
        ]
