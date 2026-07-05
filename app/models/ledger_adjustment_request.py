"""``ledger_adjustment_request`` — maker-checker for manual postings.

``AdjustmentPostingRule`` is reachable **only** through an ``approved`` row.
``created_by='admin'`` plus a free ``adjustment`` purpose otherwise means a human can post
arbitrary entries against merchant money with no maker-checker, no reason code and no
approval record — and ``payzeno_ledger`` has no ``audit_log`` table of its own to fall
back on.

``chk_adjustment_dual_control`` is the database's word on it: the approver cannot be the
requester. Violating it raises :class:`DualControlRequiredError` (403) from
``AdjustmentService.approve``, which checks it in Python first so the operator gets a
sentence rather than an ``IntegrityError``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar, Final

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Currency, LivemodeMixin, ledger_adjustment_status_enum

#: Reason codes finance will accept. Free text here means an unqueryable adjustments
#: report, and the monthly close needs to bucket them.
REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "duplicate_settlement_reversal",
        "acquirer_file_correction",
        "goodwill_credit",
        "fee_correction",
        "fx_rounding",
        "write_off",
        "migration_correction",
    }
)


class LedgerAdjustmentRequest(Base, LivemodeMixin):
    """A requested manual posting, and its approval trail."""

    __tablename__ = "ledger_adjustment_request"
    entity_name: ClassVar[str] = "ledger_adjustment_request"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)

    #: The requested legs: a list of {account_type, direction, amount_minor}. Decoded by
    #: `AdjustmentPostingRule.from_json_lines`, which rejects a half-typed request at
    #: approval time rather than at posting time — the approver is the one who can still
    #: fix it.
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)

    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    posted_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        ledger_adjustment_status_enum, nullable=False, server_default="pending"
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "approved_by is null or approved_by <> requested_by",
            name="dual_control",
        ),
        Index(
            "pix_ledger_adjustment_pending",
            "requested_at",
            postgresql_where="status = 'pending'",
        ),
        Index(
            "ix_ledger_adjustment_merchant",
            "merchant_id",
            "requested_at",
            postgresql_ops={"requested_at": "DESC"},
        ),
    )

    def is_postable(self) -> bool:
        """Only an approved, unposted request may reach ``LedgerPoster``."""
        return self.status == "approved" and self.posted_transaction_id is None

    def balance_delta_minor(self) -> int:
        """Debits minus credits across the requested legs.

        ``AdjustmentService`` checks this is zero before it even records the request, so
        an unbalanced adjustment is rejected at request time rather than surviving until
        an approver signs off on something that cannot post.
        """
        delta = 0
        for line in self.lines:
            amount = int(line.get("amount_minor", 0))
            delta += amount if line.get("direction") == "debit" else -amount
        return delta

    def has_valid_reason(self) -> bool:
        """Whether ``reason_code`` is one finance will accept."""
        return self.reason_code in REASON_CODES
