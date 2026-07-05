"""``ledger_adjustment_request`` data access — maker-checker for manual postings.

``AdjustmentPostingRule`` is reachable **only** through an ``approved`` row here. Without
that, ``created_by='admin'`` plus a free ``adjustment`` purpose means a human can post
arbitrary entries against merchant money with no second pair of eyes, no reason code and no
approval record — and ``payzeno_ledger`` has no ``audit_log`` table of its own to fall back
on. This table *is* the audit record for the one path that lets a person move money by
hand.

``chk_adjustment_dual_control`` (``approved_by IS NULL OR approved_by <> requested_by``)
enforces the maker-checker split at the storage layer. The service raises
``DualControlRequiredError`` before it gets there; the constraint is what makes the promise
true even for a psql session.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.ledger_adjustment_request import LedgerAdjustmentRequest
from app.repositories.base import BaseRepository


class LedgerAdjustmentRequestRepository(BaseRepository[LedgerAdjustmentRequest]):
    """Reads and writes adjustment requests through their maker-checker lifecycle."""

    model: ClassVar[type[LedgerAdjustmentRequest]] = LedgerAdjustmentRequest
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return LedgerAdjustmentRequest.requested_at

    async def list_pending(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[LedgerAdjustmentRequest]:
        """Requests waiting for a second approver, oldest first.

        Uses ``pix_ledger_adjustment_pending``. This is the finance queue; an entry that
        has been sitting here for a week usually means the requester is the only person on
        the team with the role, which is a staffing answer and not a software one.
        """
        stmt = (
            select(LedgerAdjustmentRequest)
            .where(LedgerAdjustmentRequest.status == "pending")
            .order_by(LedgerAdjustmentRequest.requested_at)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_for_merchant(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        limit: int = 50,
    ) -> list[LedgerAdjustmentRequest]:
        """One merchant's adjustment history, newest first.

        Uses ``ix_ledger_adjustment_merchant``. Support reads it when a merchant asks why
        their balance moved without a payment behind it — which is the whole reason
        ``reason_code`` is mandatory.
        """
        stmt = (
            select(LedgerAdjustmentRequest)
            .where(LedgerAdjustmentRequest.merchant_id == merchant_id)
            .order_by(LedgerAdjustmentRequest.requested_at.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def approve(
        self,
        session: AsyncSession,
        request_id: str,
        *,
        approved_by: str,
        at: dt.datetime | None = None,
    ) -> LedgerAdjustmentRequest:
        """Record a second person's approval.

        The service checks ``approved_by != requested_by`` and raises
        ``DualControlRequiredError`` first, so this method should never be the thing that
        trips ``chk_adjustment_dual_control``. If it ever is, the constraint has caught a
        code path that skipped the service — and it should.
        """
        record = await self.get_or_raise(session, request_id)
        record.status = "approved"
        record.approved_by = approved_by
        record.approved_at = at or dt.datetime.now(dt.UTC)
        record.updated_at = record.approved_at
        await session.flush()
        return record

    async def reject(
        self,
        session: AsyncSession,
        request_id: str,
        *,
        rejected_by: str,
        at: dt.datetime | None = None,
    ) -> LedgerAdjustmentRequest:
        """Reject a request. Terminal; a rejected request is re-filed, never re-opened.

        ``approved_by`` carries the rejecter's identity, which is a small abuse of the
        column and is why the status is what decides the meaning. Splitting it into a
        separate ``decided_by`` is a three-line migration nobody has prioritised.
        """
        record = await self.get_or_raise(session, request_id)
        record.status = "rejected"
        record.approved_by = rejected_by
        record.approved_at = at or dt.datetime.now(dt.UTC)
        record.updated_at = record.approved_at
        await session.flush()
        return record

    async def mark_posted(
        self,
        session: AsyncSession,
        request_id: str,
        *,
        transaction_id: str,
        at: dt.datetime | None = None,
    ) -> LedgerAdjustmentRequest:
        """Attach the transaction that carried out an approved adjustment.

        Stamped in the same transaction as the posting, so ``posted`` and the entries
        commit together. A request in ``approved`` with no ``posted_transaction_id`` has
        definitely not moved any money.
        """
        record = await self.get_or_raise(session, request_id)
        record.status = "posted"
        record.posted_transaction_id = transaction_id
        record.updated_at = at or dt.datetime.now(dt.UTC)
        await session.flush()
        return record

    async def find_by_transaction(
        self, session: AsyncSession, transaction_id: str
    ) -> LedgerAdjustmentRequest | None:
        """The request behind an ``adjustment`` transaction.

        The reverse lookup the ops CLI does when someone asks who authorised a posting.
        Every ``adjustment`` transaction has exactly one of these, or it should not exist.
        """
        stmt = select(LedgerAdjustmentRequest).where(
            LedgerAdjustmentRequest.posted_transaction_id == transaction_id
        )
        return (await session.execute(stmt)).scalars().first()
