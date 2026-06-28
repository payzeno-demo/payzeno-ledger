"""Invoice line staging — arc MIG step 4, in flight.

payzeno-billing-legacy still owns invoices. During the cutover its
``MigratedInvoiceService`` dual-writes every line it computes into
``invoice_line_staging`` here, so that when ``ledger_owns_invoices`` flips the ledger
already holds the history and the switch is a flag flip rather than a backfill.

Nothing downstream reads this table yet. That is the point of a staging table and it is
also why it has no indexes worth the name — when the flag flips, it gets some.

The open architectural argument, recorded here because it keeps coming back in review:
the ledger stages invoice *lines* but not invoice *numbering*. Numbering is a
jurisdictional, gap-free sequence and moving it means owning the compliance story too.
See ``docs/adr/0007-strangle-billing-legacy.md``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ids import new_id
from app.errors import ValidationError
from app.logging import get_logger
from app.metrics import metrics
from app.models.invoice_line_staging import InvoiceLineStaging
from app.ports import Clock
from app.repositories.invoice_staging import InvoiceLineStagingRepository

logger = get_logger(__name__)

#: One push from the Java service may not exceed this. Their batch job has no paging.
MAX_LINES_PER_STAGE = 5000


class InvoiceStagingService:
    """Accepts invoice lines from the legacy billing service and stores them verbatim."""

    def __init__(
        self, staging: InvoiceLineStagingRepository, clock: Clock
    ) -> None:
        self._staging = staging
        self._clock = clock

    async def stage_lines(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        invoice_public_id: str,
        period_start: date,
        period_end: date,
        currency: str,
        lines: Sequence[dict[str, Any]],
    ) -> int:
        """Replace the staged set for one invoice. Returns how many rows were written.

        Replace, not append: the Java side recomputes the whole invoice on every dunning
        pass and pushes the result, so appending would multiply every line by the number
        of passes.
        """
        if not lines:
            raise ValidationError(
                "an invoice stage push carried no lines",
                merchant_id=merchant_id,
                invoice_public_id=invoice_public_id,
            )
        if len(lines) > MAX_LINES_PER_STAGE:
            raise ValidationError(
                "too many invoice lines in one push",
                invoice_public_id=invoice_public_id,
                line_count=len(lines),
                max_lines=MAX_LINES_PER_STAGE,
            )
        if period_end < period_start:
            raise ValidationError(
                "invoice period ends before it starts",
                invoice_public_id=invoice_public_id,
                period_start=period_start.isoformat(),
                id=new_id("ils"),
                merchant_id=merchant_id,
                invoice_public_id=invoice_public_id,
                period_start=period_start,
                period_end=period_end,
                currency=currency,
                line_index=index,
                description=str(line.get("description", ""))[:255],
                amount_minor=int(line.get("amount_minor", 0)),
                tax_minor=int(line.get("tax_minor", 0)),
                staged_at=staged_at,
            )
            for index, line in enumerate(lines)
        ]
        await self._staging.add_all(session, rows)

        metrics.increment("InvoiceLinesStaged", currency=currency)
        logger.info(
            "invoice_lines_staged",
            merchant_id=merchant_id,
            invoice_public_id=invoice_public_id,
            line_count=len(rows),
        )
        return len(rows)

    async def list_staged_lines(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        invoice_public_id: str,
    ) -> list[dict[str, Any]]:
        rows = await self._staging.list_for_invoice(
            session, merchant_id=merchant_id, invoice_public_id=invoice_public_id
        )
        return [
            {
                "description": row.description,
                "quantity": row.quantity,
                "unit_amount_minor": row.unit_amount_minor,
                "amount_minor": row.amount_minor,
                "tax_minor": row.tax_minor,
                "currency": row.currency,
                "period_start": row.period_start.isoformat(),
                "period_end": row.period_end.isoformat(),
            }
            for row in rows
        ]

    async def staged_total_minor(
        self, session: AsyncSession, *, merchant_id: str, invoice_public_id: str
    ) -> int:
        rows = await self._staging.list_for_invoice(
            session, merchant_id=merchant_id, invoice_public_id=invoice_public_id
        )
        return sum(row.amount_minor + row.tax_minor for row in rows)
