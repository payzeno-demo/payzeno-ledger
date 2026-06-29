"""`app/api/routers/invoices.py` — api-surface.md §10.6, arc MIG step 4.

The two routes `payzeno-billing-legacy`'s `MigratedInvoiceService` calls. Without them
`PAYZENO_LEDGER_BASE_URL`, `LedgerSyncException` and the whole
`payzeno-billing-legacy → payzeno-ledger` edge point at nothing.

This is the unfinished half of the strangler. The Java service stages lines here on every
invoice run — a dual write — while remaining the source of truth, and the cutover is a
flag flip on the **caller** side (`ledger_owns_invoices`, off). `invoice_line_staging`
therefore fills up in production and is read by nobody, which is exactly what a dual write
is supposed to look like and is also why it has never had a bug anyone noticed.

The open architectural question, and it is a real disagreement in review: whether the
ledger should own invoice *numbering* as well as lines. It does not today. These tests
assume it does not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.api.routers.invoices import list_staged_lines, stage_lines
from app.errors import ValidationError

pytestmark = pytest.mark.asyncio

#: The internal caller `require_internal_service` resolved. For this route it is always
#: payzeno-billing-legacy — arc MIG step 4 pushes lines here from `MigratedInvoiceService`.
CALLER = "payzeno-billing-legacy"

NOW = datetime(2026, 4, 16, 18, 0, tzinfo=UTC)
PERIOD_START = date(2026, 3, 1)
PERIOD_END = date(2026, 3, 31)


class StubStaging:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.staged: list[dict[str, Any]] = []
        self.raises = raises

    async def stage_lines(self, session: Any, **kwargs: Any) -> int:
        if self.raises is not None:
            raise self.raises
        self.staged.append(kwargs)
        return len(kwargs["lines"])

    async def list_staged_lines(
        self, session: Any, *, merchant_id: str, invoice_public_id: str
    ) -> list[dict[str, Any]]:
        return [
            row
            for call in self.staged
            if call["merchant_id"] == merchant_id
            and call["invoice_public_id"] == invoice_public_id
            for row in call["lines"]
        ]


def _body(**kwargs: Any) -> Any:
    payload = {
        "merchant_id": "mer_inv",
        "invoice_public_id": "INV-2026-0331",
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "currency": "USD",
        "lines": [
            {"description": "Processing fees — March", "amount_minor": 41_820, "quantity": 1},
            {"description": "Chargeback fees", "amount_minor": 4_500, "quantity": 3},
        ],
    }
    payload.update(kwargs)
    return type("StageLinesRequest", (), payload)()


async def test_staging_returns_the_count(sessions_factory) -> None:
    staging = StubStaging()

    response = await stage_lines(_body(), sessions_factory, staging, CALLER)

    assert response["staged"] == 2


async def test_staging_carries_the_invoice_public_id(sessions_factory) -> None:
    """The Java side's identifier, not ours.

    The ledger does not mint invoice numbers today. If it ever does, this is the field
    that changes meaning and this is the test that will notice.
    """
    staging = StubStaging()

    await stage_lines(_body(), sessions_factory, staging, CALLER)

    assert staging.staged[0]["invoice_public_id"] == "INV-2026-0331"


async def test_staging_is_scoped_to_a_period(sessions_factory) -> None:
    staging = StubStaging()

    await stage_lines(_body(), sessions_factory, staging, CALLER)

    assert staging.staged[0]["period_start"] == PERIOD_START
    assert staging.staged[0]["period_end"] == PERIOD_END


async def test_an_empty_line_set_is_rejected(sessions_factory) -> None:
    """An invoice with no lines is a bug on the caller's side, not a valid dual write."""
    staging = StubStaging(raises=ValidationError("an invoice needs at least one line"))

    with pytest.raises(ValidationError):
        await stage_lines(_body(lines=[]), sessions_factory, staging, CALLER)


async def test_listing_returns_what_was_staged(sessions_factory) -> None:
    staging = StubStaging()
    await stage_lines(_body(), sessions_factory, staging, CALLER)

    response = await list_staged_lines(sessions_factory, staging, "mer_inv", "INV-2026-0331")

    assert len(response["lines"]) == 2
    assert response["lines"][0]["amount_minor"] == 41_820


async def test_listing_is_scoped_to_one_merchant(sessions_factory) -> None:
    staging = StubStaging()
    await stage_lines(_body(), sessions_factory, staging, CALLER)
    await stage_lines(
        _body(merchant_id="mer_other", invoice_public_id="INV-2026-0332"),
        sessions_factory,
        staging,
        CALLER,
    )

    response = await list_staged_lines(sessions_factory, staging, "mer_inv", "INV-2026-0331")

    assert len(response["lines"]) == 2


async def test_listing_an_unstaged_invoice_returns_an_empty_set(sessions_factory) -> None:
    """Not a 404. With the flag off, most invoices have never been staged.

    A 404 here would make the Java side's reconciliation job log an error per invoice for
    every invoice run, which is how you teach a team to ignore that log.
    """
    staging = StubStaging()

    response = await list_staged_lines(sessions_factory, staging, "mer_inv", "INV-2026-9999")

    assert response["lines"] == []


async def test_re_staging_the_same_invoice_replaces_rather_than_appends(
    sessions_factory,
) -> None:
    """The Java job reruns when an invoice is regenerated.

    Appending would double every line, and since nothing reads the table with the flag
    off, nobody would find out until the cutover.
    """
    staging = StubStaging()

    await stage_lines(_body(), sessions_factory, staging, CALLER)
    response = await stage_lines(_body(), sessions_factory, staging, CALLER)

    assert response["staged"] == 2
