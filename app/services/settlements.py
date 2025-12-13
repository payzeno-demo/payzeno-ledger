"""Settlement batch lifecycle and file import.

Nothing else in payzeno-ledger opens a batch or creates a ``reconciliation_item``. The
whole reconciliation subsystem is downstream of this module, which is why an import that
half-runs is worse than one that does not run at all: every item this file fails to
create is money the ledger will never notice is missing.

Two entry points, and they are not equivalent:

* :class:`SettlementImportService` — the modern path. Pulls the file from the acquirer,
  parses it, opens the batch, matches, closes.
* :meth:`SettlementService.import_legacy_records` — the ``POST /internal/v1/settlement-imports``
  route payzeno-billing-legacy's ``LedgerReconciliationExportJob`` pushes to. It predates
  the import service and duplicates a chunk of its matching, because when it was written
  there was nothing to reuse.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

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
            processing_date=processing_date,
            file_reference=file_reference,
            status="open",
            livemode=livemode,
            opened_at=self._clock.now(),
        )
        await self._batches.add(session, batch)
        logger.info("settlement_batch_opened", batch_id=batch.id, acquirer=acquirer)
        return batch

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
            correlation_id=batch.id,
            batch_id=batch.id,
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
            file_reference=file_reference,
        )

        items: list[ReconciliationItem] = []
        for record in records:
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
            acquirer=acquirer, processing_date=processing_date
        )
        parsed = parser_for(acquirer).parse(raw)
        if not parsed:
            logger.warning(
                "settlement_file_empty",
                acquirer=acquirer,
                processing_date=processing_date.isoformat(),
            )

        file_reference = f"{acquirer}-{processing_date.isoformat()}"
        currency = parsed[0].currency if parsed else "USD"

        async with self._sessions.begin() as session:
            batch = await self._settlements.open_batch(
                session,
                acquirer=acquirer,
                currency=currency,
                processing_date=processing_date,
                file_reference=file_reference,
            )
            batch_id = batch.id

        async with self._sessions.begin() as session:
            batch_id=batch_id,
            acquirer=acquirer,
        ]
