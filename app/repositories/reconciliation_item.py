"""``reconciliation_item`` data access — the hottest file in this package.

Three of the methods below have history attached and it is worth knowing which:

* :meth:`ReconciliationItemRepository.list_for_settlement` is the batch sweep's read. A
  plain ``SELECT``, no row locks. The sweep excludes other sweeps with the *batch*
  advisory lock, not with row locks, so locking here would be redundant against another
  sweep — and it is exactly why a retry holding ``FOR UPDATE`` on one of these rows
  skipped nothing.
* :meth:`ReconciliationItemRepository.list_retryable_ids` is the retry drain's read. It
  scans ``pix_reconciliation_item_retryable`` and filters ``next_attempt_at <= now()``.
  Before migration ``0014`` this sequential-scanned 2.4M rows at a p99 of twelve seconds,
  which is the only reason the drain used to fall so far behind the sweep that the two
  never overlapped. Making it fast (arc PERF, month 7) is what made them concurrent.
* :meth:`ReconciliationItemRepository.get_batch_id` was added by PAY-2043 at 02:00 on the
  night of the incident, so ``RetryScheduler._claim_item`` could ask for the batch
  advisory lock *before* it took the row lock. Four lines, and it is the fix.

There is deliberately **no unique index on ``charge_id``** and there must not be: a charge
legitimately appears in two batches (the original and a chargeback representment). A
reviewer who looks only at this table concludes the design is safe, and that is precisely
what happened.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ReconciliationItemNotFoundError
from app.models.reconciliation_item import ReconciliationItem
from app.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class BatchTotals:
    """Aggregate of one batch's items, used when the import service closes the batch."""

    item_count: int
    gross_minor: int
    fee_minor: int
    net_minor: int


@dataclass(frozen=True, slots=True)
class BacklogRow:
    """One ``(batch, status)`` bucket of the retry backlog.

    ``oldest_next_attempt_at`` is what makes a bucket *stale* rather than merely large:
    a batch whose oldest item is due in four minutes is working, and a batch whose oldest
    item came due ninety minutes ago is stuck behind something.
    """

    batch_id: str
    currency: str
    status: str
    item_count: int
    gross_minor: int | None
    oldest_next_attempt_at: dt.datetime | None


class ReconciliationItemRepository(BaseRepository[ReconciliationItem]):
    """Reads and writes ``reconciliation_item`` rows.

    Created only by ``SettlementImportService`` (and by the legacy import path), read by
    the sweep, the drain, the backlog endpoint and the ops CLI.
    """

    model: ClassVar[type[ReconciliationItem]] = ReconciliationItem
    not_found_error: ClassVar[type[ReconciliationItemNotFoundError]] = (
        ReconciliationItemNotFoundError
    )

    def _default_order(self) -> ColumnElement[Any]:
        return ReconciliationItem.id

    async def list_for_settlement(
        self,
        session: AsyncSession,
        *,
        batch_id: str,
        statuses: frozenset[str],
        limit: int,
    ) -> list[ReconciliationItem]:
        """Items in one batch eligible for settlement, oldest first.

        ``statuses`` is always ``RETRYABLE_STATUSES`` from
        ``app/services/reconciliation/constants.py`` — the same frozenset the retry
        scheduler imports, so the two paths cannot disagree about what is eligible. They
        agree about eligibility and they used to disagree about locking, which is the
        entire shape of PAY-2041.

        No ``FOR UPDATE``. The caller holds the batch advisory lock; see the module
        docstring, and `docs/adr/0011-lock-ordering-in-the-money-path.md`.
        """
        stmt = (
            select(ReconciliationItem)
            .where(ReconciliationItem.batch_id == batch_id)
            .where(ReconciliationItem.status.in_(tuple(statuses)))
            .order_by(ReconciliationItem.created_at)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_retryable_ids(
        self,
        session: AsyncSession,
        *,
        limit: int,
        stmt = (
            select(ReconciliationItem.id)
            .where(ReconciliationItem.status.in_(("pending", "retryable")))
            .where(ReconciliationItem.next_attempt_at <= cutoff)
            .order_by(ReconciliationItem.next_attempt_at)
            .limit(limit)
        )
        return [row[0] for row in (await session.execute(stmt)).all()]

    async def get_batch_id(self, session: AsyncSession, item_id: str) -> str | None:
        """The parent batch id, or ``None`` when the item does not exist.

        Added by PAY-2043 (PR #171, merged 02:00). ``RetryScheduler._claim_item`` calls
        this first so it can take ``AdvisoryLockManager.try_acquire_batch_lock`` before
        the row lock — advisory-then-row, the same order the sweep uses, which is the
        whole content of the hotfix.

        Returns ``None`` rather than raising: ``_claim_item`` treats a missing item as
        "not claimable" and returns ``None`` to its caller, which the route maps onto
        ``409 settlement_locked``. Raising here would turn a lost race into a 500.
        """
        item = await self.get_or_raise(session, item_id)
        item.status = status
        if error_code is not None:
            item.last_error_code = error_code
        if at is not None:
            item.last_attempt_at = at
        await session.flush()
        return item

    async def count_by_status(
        self, session: AsyncSession, *, batch_id: str | None = None
    ) -> dict[str, int]:
        """Item counts per status, for one batch or the whole table.

        Backs the batch detail page and the ops CLI's ``batch`` command. Cheap under
        ``ix_reconciliation_item_batch_status``.
        """
        stmt = select(ReconciliationItem.status, func.count()).group_by(
            ReconciliationItem.status
        )
        if batch_id is not None:
            stmt = stmt.where(ReconciliationItem.batch_id == batch_id)
        return {row[0]: int(row[1]) for row in (await session.execute(stmt)).all()}

    async def totals_for_batch(self, session: AsyncSession, batch_id: str) -> BatchTotals:
        """Sum a batch's lines, for ``SettlementService.close_batch``.

        ``expected_total_minor`` on the batch is the sum of the items' ``net_minor`` —
        what the acquirer says it will pay — and the funding matcher later compares a real
        bank credit against it within ``FUNDING_MATCH_TOLERANCE_BPS``.
        """
        stmt = select(
            func.count(),
            func.coalesce(func.sum(ReconciliationItem.gross_minor), 0),
            func.coalesce(func.sum(ReconciliationItem.fee_minor), 0),
            func.coalesce(func.sum(ReconciliationItem.net_minor), 0),
        ).where(ReconciliationItem.batch_id == batch_id)
        """
        stmt = (
            select(
                ReconciliationItem.batch_id,
                ReconciliationItem.currency,
                ReconciliationItem.status,
                func.count().label("item_count"),
                func.coalesce(func.sum(ReconciliationItem.gross_minor), 0),
                func.min(ReconciliationItem.next_attempt_at),
            )
            .where(ReconciliationItem.status.in_(tuple(statuses)))
            .group_by(
                ReconciliationItem.batch_id,
                ReconciliationItem.currency,
                ReconciliationItem.status,
            )
            .order_by(func.count().desc())
        )
        if currency is not None:
            stmt = stmt.where(ReconciliationItem.currency == currency)
        if batch_id is not None:
            stmt = stmt.where(ReconciliationItem.batch_id == batch_id)

        return [
            BacklogRow(
                batch_id=row[0],
                currency=row[1],
                status=row[2],
                item_count=int(row[3]),
                gross_minor=int(row[4] or 0),
                oldest_next_attempt_at=row[5],
            )
            for row in (await session.execute(stmt)).all()
        ]

    async def list_by_charge(
        self, session: AsyncSession, charge_id: str
    ) -> list[ReconciliationItem]:
        """Every item referencing one charge, across batches.

        Returns a list and not an item on purpose — see the module docstring. The
        duplicate-finding query in `docs/runbooks/reconciliation.md` starts here.
        """
        stmt = (
            select(ReconciliationItem)
            .where(ReconciliationItem.charge_id == charge_id)
            .order_by(ReconciliationItem.created_at)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_needing_review(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[ReconciliationItem]:
        """Items a human has to look at: heuristic matches, variances and orphans.

        None of these ever auto-settle. ``needs_review`` in particular is what the
        heuristic amount-window match produces — a hint, not an answer.
