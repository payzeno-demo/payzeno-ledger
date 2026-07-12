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
        stmt = (
            select(SettlementBatch)
            .where(SettlementBatch.status.in_(statuses))
            .order_by(SettlementBatch.processing_date)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_unfunded(
        self,
        session: AsyncSession,
        *,
        acquirer: str | None = None,
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
        acquirer: str | None = None,
    ) -> list[SettlementBatch]:
        """Every batch for one calendar processing day.

        One acquirer files one batch per currency per day, so this is usually five rows.
        The ops CLI prints it as a table when someone asks "did yesterday land".
        """
        stmt = select(SettlementBatch).where(
            SettlementBatch.processing_date == processing_date
        )
        if acquirer is not None:
            stmt = stmt.where(SettlementBatch.acquirer == acquirer)
        stmt = stmt.order_by(SettlementBatch.currency)
        return list((await session.execute(stmt)).scalars().all())

    async def add_posted_total(
        self, session: AsyncSession, batch_id: str, *, amount_minor: int
    ) -> SettlementBatch:
        """Accumulate ``posted_total_minor`` by one item's net.

        Read-modify-write on a hydrated row rather than an in-place SQL ``UPDATE ... SET
        posted_total_minor = posted_total_minor + :n``, because every caller is already
        inside the transaction that posted the item and holds the batch advisory lock, so
        there is no concurrent writer to lose an increment to. If that ever stops being
        true this is the line that starts undercounting, which is why it says so here.
        """
        batch = await self.get_or_raise(session, batch_id)
        batch.posted_total_minor += amount_minor
        await session.flush()
        return batch

    async def mark_status(
        self, session: AsyncSession, batch_id: str, *, status: str
    ) -> SettlementBatch:
        """Move a batch along its state machine, stamping the matching timestamp."""
        batch = await self.get_or_raise(session, batch_id)
        batch.status = status
        await session.flush()
        return batch

    async def mark_reconciled(
        self,
        session: AsyncSession,
        batch_id: str,
        *,
        posted_total_minor: int,
        fully_settled: bool,
        at: dt.datetime,
    ) -> SettlementBatch:
        """Close out a reconciliation pass over one batch.

        ``fully_settled`` decides between ``reconciled`` and ``partially_reconciled``.
        The partial state is not a failure — items land in ``retryable`` for entirely
        ordinary acquirer 504s — and the batch stays eligible for the next sweep, which is
        how a backlog survives long enough for a drain to be working the same rows.

        ``posted_total_minor`` is set rather than accumulated here: the pass knows its own
        total and the batch may have been partially posted by an earlier pass, so adding
        would double-count everything settled before this one.
        """
        batch = await self.get_or_raise(session, batch_id)
        batch.posted_total_minor = posted_total_minor
        batch.status = "reconciled" if fully_settled else "partially_reconciled"
        if fully_settled:
            batch.reconciled_at = at
        await session.flush()
        return batch

    async def mark_funded(
        self,
        session: AsyncSession,
        batch_id: str,
        *,
        funding_event_id: str,
        funded_amount_minor: int,
        at: dt.datetime,
    ) -> SettlementBatch:
        """Record that real money arrived for this batch.

        ``funded`` is terminal and it is the only status ``PayoutCalculator`` counts. The
        ``settlement_funding`` posting — the one and only debit of ``cash`` — happens in
        the same transaction as this call.
        """
        batch = await self.get_or_raise(session, batch_id)
        batch.status = "funded"
        batch.funding_event_id = funding_event_id
        batch.funded_amount_minor = funded_amount_minor
        batch.funded_at = at
        await session.flush()
        return batch
