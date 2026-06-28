"""``invoice_line_staging`` — arc MIG step 4. In flight, and unused in production.

The strangler boundary (`domain-model.md` §13). During the cutover
``payzeno-billing-legacy`` remains the source of truth: ``MigratedInvoiceService``
dual-writes, generating the invoice locally *and* calling
``POST /internal/v1/invoices/lines/stage`` on this service. This table accumulates those
lines.

It is written **only** by the ledger, **only** from that route, and is unused in production
while the ``ledger_owns_invoices`` flag is off — which it is, and has been since month 12
started. Flipping the flag changes exactly one thing: ``payzeno_api.invoice_projection``'s
writer moves from the ``invoice.issued`` event to ``LedgerHttpClient``, and the legacy
generator stops. That swap is what is unfinished at demo time.

The open architectural disagreement in review is whether the ledger should own invoice
*numbering* as well as lines. ``dhotfix`` says yes; ``nmigration`` says numbering is a
compliance surface with a per-country format and should stay where the tax logic already
lives. Which is why :attr:`invoice_number` here is nullable and carries the legacy
service's number rather than one of ours.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import BigInteger, Boolean, Date, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Currency, LivemodeMixin, TimestampMixin


class InvoiceLineStaging(Base, TimestampMixin, LivemodeMixin):
    """One staged invoice line, dual-written by the Java biller during the cutover."""

    __tablename__ = "invoice_line_staging"
    entity_name: ClassVar[str] = "invoice_line_staging"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(Text, nullable=False)

    #: The legacy service's `invoice.public_id`. The ledger does not mint invoice ids.
    source_invoice_public_id: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Nullable on purpose — see the module docstring. Numbering has not moved.
    invoice_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: `invoice_line_item.line_no` on the legacy side. Half of the natural key.
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)

    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The legacy side stores quantity as decimal(12,4) and there is no minor-unit
    #: equivalent for a count, so this is the one Numeric column in payzeno_ledger.
    quantity: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False, server_default="1")
    unit_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(Currency, nullable=False)

    period_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    #: The legacy `fee_schedule.public_id` the line was priced from, so a parity check can
    #: reconcile the ledger's own fee computation against what the biller charged. This is
    #: the column V33 was added on the Java side to supply.
    source_fee_schedule_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    #: Whatever the legacy payload carried that the ledger has no column for yet. Step 4
    #: is unfinished; this is where the unmapped remainder goes so nothing is silently
    #: dropped during the cutover.
    source_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    #: True once the ledger has generated its own equivalent line. Nothing sets it yet.
    promoted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        # The dual write is at-least-once: the Java side retries the stage call, and a
        # duplicated line would double the invoice total after the cutover.
        Index(
            "uq_invoice_line_staging_source_line",
            "source_invoice_public_id",
            "line_no",
            unique=True,
        ),
        Index(
            "ix_invoice_line_staging_merchant_period",
            "merchant_id",
            "period_start",
            postgresql_ops={"period_start": "DESC"},
        ),
        Index(
            "pix_invoice_line_staging_unpromoted",
            "created_at",
            postgresql_where="not promoted",
        ),
    )

    def total_minor(self) -> int:
        """Line amount including tax."""
        return self.amount_minor + self.tax_minor

    def natural_key(self) -> tuple[str, int]:
        """``(source_invoice_public_id, line_no)`` — what the unique index is on."""
        return (self.source_invoice_public_id, self.line_no)
