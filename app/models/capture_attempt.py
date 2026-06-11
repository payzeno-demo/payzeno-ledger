"""``capture_attempt`` — PAY-2060, the control that actually sits on the double-capture path.

``uq_charge_processor_reference`` in ``payzeno_api`` is **not** a defence against the INC
double capture: it lives in the other database, and the duplicate capture is issued by
this service from ``SettlementPoster.post_settlement`` against a database that holds only
a ``settlement_charge`` projection. The ledger never writes ``charge``, so that index is
never consulted on the path that double-charges.

This table is. The row is **inserted and committed before** the acquirer call, under
``uq_capture_attempt_key (acquirer, acquirer_idempotency_key)``, so a second capture fails
on insert before any HTTP request leaves the process.

``DeferredCaptureJob`` (30s) drives the ``pending`` rows: it is what makes the external
call happen **outside** the business transaction, so a transaction that aborts after the
claim cannot leave a charged cardholder with no ledger row. That is the window PR #172's
atomic upsert left wide open and PAY-2060 closed.

``indeterminate`` rows are resolved by ``ProcessorClient.get_capture_status`` — never by
re-issuing the capture. A timeout on a capture is precisely the state where you do not
know whether the cardholder was charged.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar, Final

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    acquirer_enum,
    capture_attempt_status_enum,
)

#: Statuses `DeferredCaptureJob` picks up off `pix_capture_attempt_pending`.
OPEN_STATUSES: Final[tuple[str, ...]] = ("pending", "indeterminate")

#: Invariant 6 of `data-model.md` §6: no `pending` attempt older than this is acceptable.
#: `LedgerAuditJob` emits `ledger.imbalance_detected` when one is.
STALE_PENDING_SECONDS: Final[int] = 3600


class CaptureAttempt(Base, LivemodeMixin):
    """One deferred capture request against the acquirer, claimed before it is issued."""

    __tablename__ = "capture_attempt"
    entity_name: ClassVar[str] = "capture_attempt"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    charge_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: The reconciliation item this capture belongs to. Nullable because a manual capture
    #: replay from the ops CLI has a charge but no item.
    item_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("reconciliation_item.id", ondelete="RESTRICT"), nullable=True
    )
    acquirer: Mapped[str] = mapped_column(acquirer_enum, nullable=False)
    #: Deterministic: `ledger_key("capture", batch_id, charge_id)`. Both acquirers honour
    #: it, so even a request that escapes twice is idempotent at their end — which was
    #: not true on the night of the incident, when nothing sent one at all.
    acquirer_idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    status: Mapped[str] = mapped_column(
        capture_attempt_status_enum, nullable=False, server_default="pending"
    )
    response_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # THE control. A second capture for the same (acquirer, key) fails on INSERT,
        # before any HTTP request leaves the process.
        Index(
            "uq_capture_attempt_key",
            "acquirer",
            "acquirer_idempotency_key",
            unique=True,
        ),
        Index(
            "pix_capture_attempt_pending",
            "requested_at",
            postgresql_where="status in ('pending','indeterminate')",
        ),
        Index("ix_capture_attempt_charge", "charge_id"),
        Index("ix_capture_attempt_item", "item_id"),
    )

    def is_open(self) -> bool:
        """Whether ``DeferredCaptureJob`` should act on this row."""
        return self.status in OPEN_STATUSES

    def is_stale(self, now: dt.datetime) -> bool:
        """Whether this attempt has been pending longer than invariant 6 allows."""
        if self.status != "pending":
            return False
        return (now - self.requested_at).total_seconds() > STALE_PENDING_SECONDS

    def needs_status_probe(self) -> bool:
        """``indeterminate`` resolves through ``get_capture_status``, never a re-issue."""
        return self.status == "indeterminate"
