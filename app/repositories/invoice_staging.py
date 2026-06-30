"""``invoice_line_staging`` data access — arc MIG step 4, in flight.

The Java biller still owns invoices. What it has started doing is pushing the *lines* here,
through ``POST /internal/v1/invoices/lines/stage``, so that the ledger becomes the source
of line-level truth before it becomes the source of invoice-level truth. Nothing in
production reads these rows yet; migration ``0022`` created the table in month 12 and the
promotion path is unwritten.

Whether the ledger should also own invoice *numbering* is the open architectural argument
on this arc. Until that is settled, ``invoice_number`` here is a copy of the legacy value
and nothing generates one.

The natural key is ``(merchant_id, source_invoice_public_id, line_no)`` — the Java side
re-pushes an invoice whenever it is edited, and re-pushing must replace rather than
duplicate.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.invoice_line_staging import InvoiceLineStaging
from app.repositories.base import BaseRepository


class InvoiceLineStagingRepository(BaseRepository[InvoiceLineStaging]):
    """Reads and writes staged invoice lines."""

    model: ClassVar[type[InvoiceLineStaging]] = InvoiceLineStaging
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return InvoiceLineStaging.id

    async def list_for_invoice(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        invoice_public_id: str,
    ) -> list[InvoiceLineStaging]:
        """Every staged line for one legacy invoice, in line order.

        ``line_no`` and not ``created_at``: the Java side numbers its lines and an invoice
        whose lines come back in insertion order reads wrong the first time somebody edits
        line 2 and re-pushes.
        """
        stmt = (
            select(InvoiceLineStaging)
            .where(InvoiceLineStaging.merchant_id == merchant_id)
            .where(InvoiceLineStaging.source_invoice_public_id == invoice_public_id)
            .order_by(InvoiceLineStaging.line_no)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_unpromoted(
        self,
        session: AsyncSession,
        *,
        merchant_id: str | None = None,
        limit: int = 200,
    ) -> list[InvoiceLineStaging]:
        """Lines that have not been turned into ledger postings.

        Uses ``pix_invoice_line_staging_unpromoted``. Everything is unpromoted today,
        because the promotion path does not exist — which is exactly what the partial
        index is for once it does.
        """
        stmt = (
            select(InvoiceLineStaging)
            .where(InvoiceLineStaging.promoted.is_(False))
            .order_by(InvoiceLineStaging.created_at)
            .limit(limit)
        )
        if merchant_id is not None:
            stmt = stmt.where(InvoiceLineStaging.merchant_id == merchant_id)
        return list((await session.execute(stmt)).scalars().all())

    async def mark_promoted(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        invoice_public_id: str,
        at: dt.datetime | None = None,
    ) -> int:
        """Flag an invoice's lines as promoted. Returns the row count.

        Called by nothing in production yet. It is here because the flag is in the schema
        and a column with no writer is worse than an unused method — the next person reads
        ``promoted`` and assumes something maintains it.
        """
        stmt = (
            InvoiceLineStaging.__table__.update()
            .where(InvoiceLineStaging.merchant_id == merchant_id)
            .where(InvoiceLineStaging.source_invoice_public_id == invoice_public_id)
            .where(InvoiceLineStaging.promoted.is_(False))
            .values(promoted=True, updated_at=at or dt.datetime.now(dt.UTC))
        )
        return int((await session.execute(stmt)).rowcount or 0)

    async def sum_for_invoice(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        invoice_public_id: str,
    ) -> int:
        """Total of an invoice's staged lines, tax included, in minor units.

        The parity check against the Java side's ``decimal(19,4)`` total. Any difference is
        the legacy scaling bug arc MIG exists to remove, and it is reported rather than
        corrected — silently agreeing with a number computed in floating-decimal would
        destroy the only evidence the migration is worth doing.
        """
        stmt = (
            select(
                func.coalesce(
                    func.sum(
                        InvoiceLineStaging.amount_minor + InvoiceLineStaging.tax_minor
                    ),
                    0,
                )
            )
            .where(InvoiceLineStaging.merchant_id == merchant_id)
            .where(InvoiceLineStaging.source_invoice_public_id == invoice_public_id)
        )
        return int((await session.execute(stmt)).scalar_one())
