"""``/internal/v1/invoices/lines`` — arc MIG step 4, in flight.

payzeno-billing-legacy's ``MigratedInvoiceService#generate`` dual-writes here: it still
generates the invoice itself, and it stages the *lines* with us so that flipping
``ledger_owns_invoices`` on the caller side is the whole cutover. Nothing reads these
lines in production yet except ``MigratedInvoiceService#reconcileLines``, which compares
them against its own and logs a divergence.

The open architectural question — and it is genuinely open, in review on ledger PR #168 —
is whether the ledger should own invoice *numbering* as well as lines. Numbering is
jurisdictional (sequential, gapless, per legal entity) and the Java service has eight
years of it. Staging lines does not commit us either way, which is why it landed first.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    InternalCaller,
    get_invoice_service,
    get_sessions,
    require_internal_service,
)
from app.api.schemas import (
    StageInvoiceLinesRequest,
    StageInvoiceLinesResponse,
    StagedInvoiceLinesResponse,
)
from app.errors import ValidationError
from app.logging import get_logger
from app.ports import SessionFactory
from app.services.invoices import InvoiceStagingService

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/invoices",
    tags=["invoices"],
    dependencies=[Depends(require_internal_service)],
)

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]
InvoicesDep = Annotated[InvoiceStagingService, Depends(get_invoice_service)]

#: One invoice's worth of lines. A monthly interchange-plus invoice for a large merchant
#: runs to a few thousand rows; beyond this the Java service is sending us something
#: other than an invoice.
MAX_LINES_PER_REQUEST = 10_000


@router.post(
    "/lines/stage",
    response_model=StageInvoiceLinesResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stage the lines of one invoice",
)
async def stage_lines(
    body: StageInvoiceLinesRequest,
    sessions: SessionsDep,
    invoices: InvoicesDep,
    caller: InternalCaller,
) -> dict[str, int]:
    """Replaces any previously staged lines for the same ``invoice_public_id``.

    Replace rather than append: the Java service regenerates an invoice wholesale when a
    credit note or a late fee lands, and an append would leave us holding both versions
    with no way to tell which is current. ``InvoiceStagingService.stage_lines`` deletes
    then inserts inside one transaction.
    """
    if not body.lines:
        raise ValidationError(
            "at least one line is required",
            invoice_public_id=body.invoice_public_id,
        )
    if len(body.lines) > MAX_LINES_PER_REQUEST:
        raise ValidationError(
            f"at most {MAX_LINES_PER_REQUEST} lines per request",
            period_end=body.period_end.isoformat(),
        )

    lines = [line.model_dump() for line in body.lines]
    async with sessions.begin() as session:
        staged = await invoices.stage_lines(
            session,
        )

    logger.debug(
        "invoice_lines_read",
        merchant_id=merchant_id,
        invoice_public_id=invoice_public_id,
        line_count=len(lines),
        total_minor=total_minor,
    )
    return {"lines": lines}
