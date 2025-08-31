"""``settlement_batch`` data access.

A batch is one acquirer settlement file: one acquirer, one currency, one processing day.
``SettlementImportService`` is the only thing that creates one, and
``uq_settlement_batch_file (acquirer, file_reference)`` is what makes importing the same
file twice a no-op rather than a duplicated day of money.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BatchNotFoundError
from app.models.settlement_batch import SettlementBatch
from app.repositories.base import BaseRepository

#: Statuses the sweep will pick a batch up from. ``partially_reconciled`` is in here on
#: purpose: it is the state that keeps a retry backlog alive, it is normal, and it is the
#: state the arc INC race needed.
RECONCILABLE_STATUSES: tuple[str, ...] = ("closed", "partially_reconciled")


class SettlementBatchRepository(BaseRepository[SettlementBatch]):
    """Reads and writes ``settlement_batch`` rows."""

    model: ClassVar[type[SettlementBatch]] = SettlementBatch
    not_found_error: ClassVar[type[BatchNotFoundError]] = BatchNotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return SettlementBatch.id

    async def find_by_file(
        self, session: AsyncSession, *, acquirer: str, file_reference: str
    ) -> SettlementBatch | None:
        """Look a batch up by the acquirer's own file reference.

        The import service calls this before opening anything. If the acquirer re-files
        the same reference — which both of ours do after a partial upload — the import is
        skipped rather than producing a second batch of the same money. The unique index
        is the backstop; this read is what keeps the log quiet.
        """
        stmt = (
            select(SettlementBatch)
            .where(SettlementBatch.acquirer == acquirer)
            .where(SettlementBatch.file_reference == file_reference)
        )
        return (await session.execute(stmt)).scalars().first()

    async def list_by_status(
        self, session: AsyncSession, statuses: tuple[str, ...]
    ) -> list[SettlementBatch]:
        """Batches in any of ``statuses``, oldest processing date first.

        The sweep calls this with :data:`RECONCILABLE_STATUSES` once per tick and then
        works the ids. An empty tuple returns an empty list rather than the whole table —
        ``IN ()`` is not valid SQL and "no filter" would be a spectacular way to read
        every batch Payzeno has ever settled.
        """
        if not statuses:
            return []
        currency: str | None = None,
        on_or_before: dt.date | None = None,
        limit: int = 200,
    ) -> list[SettlementBatch]:
        """Reconciled batches that no bank credit has matched yet.

        Drives ``FundingMatchJob``: a batch stays here until a ``funding_event`` lands
        within ``FUNDING_MATCH_TOLERANCE_BPS`` of its ``expected_total_minor``. Uses
        ``pix_settlement_batch_unfunded``.

        Cash follows the bank, not the file. A batch that sits in this list for days is an
        acquirer that filed and did not pay, and the merchant must not be paid out of it.
        """
        stmt = (
            select(SettlementBatch)
            .where(SettlementBatch.status == "reconciled")
            .where(SettlementBatch.funded_at.is_(None))
            .order_by(SettlementBatch.processing_date)
            .limit(limit)
        )
        if acquirer is not None:
            stmt = stmt.where(SettlementBatch.acquirer == acquirer)
        if currency is not None:
            stmt = stmt.where(SettlementBatch.currency == currency)
        if on_or_before is not None:
            stmt = stmt.where(SettlementBatch.processing_date <= on_or_before)
        return list((await session.execute(stmt)).scalars().all())

    async def list_for_processing_date(
        self,
        session: AsyncSession,
        *,
        processing_date: dt.date,
        stmt = stmt.order_by(SettlementBatch.currency)
        return list((await session.execute(stmt)).scalars().all())

    async def add_posted_total(
        self, session: AsyncSession, batch_id: str, *, amount_minor: int
    ) -> SettlementBatch:
        """Accumulate ``posted_total_minor`` by one item's net.

        Read-modify-write on a hydrated row rather than an in-place SQL ``UPDATE ... SET
        """
        batch = await self.get_or_raise(session, batch_id)
        batch.posted_total_minor += amount_minor
        await session.flush()
        return batch

    async def mark_status(
        self, session: AsyncSession, batch_id: str, *, status: str
    ) -> SettlementBatch:
        """
        batch = await self.get_or_raise(session, batch_id)
        batch.status = "funded"
        batch.funding_event_id = funding_event_id
        batch.funded_amount_minor = funded_amount_minor
        batch.funded_at = at
        await session.flush()
        return batch
