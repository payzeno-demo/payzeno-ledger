"""``payout`` — a transfer of a merchant's payable balance to their bank account.

::

    scheduled ─initiate─▶ in_transit ─▶ paid ─return─▶ returned   (terminal, ACH R-code)
         │                    │
         │                    └─▶ failed   (terminal — emits payout.failed)
         └─cancel─▶ canceled  (terminal; only while scheduled)

``chk_payout_reversal_present`` is invariant 7 of `domain-model.md` §7 at the storage
layer: a payout cannot reach ``failed`` or ``returned`` without a ``payout_reversal``
transaction. Without it an ACH failure permanently destroys the merchant's money —
``payout`` already debited ``merchant_payable``, ``failed`` is terminal, and nothing gives
it back.

``pix_payout_in_flight`` is **unique** (migration ``0033``).
``PayoutCalculator.compute_available`` subtracts in-flight payouts by reading rows a
concurrent uncommitted transaction has not written yet — the identical check-then-act
shape as PAY-2041, on the path that moves money to a bank account. ``create_payout`` takes
``acquire_merchant_currency_lock`` **before** computing the balance, and this index is the
database's last word if that is ever bypassed.

This file went cold about eight weeks ago when ``mhandover`` started handing over.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    TimestampMixin,
    payout_failure_code_enum,
    payout_method_enum,
    payout_status_enum,
)

#: Statuses that hold money outside `merchant_payable` but not yet at the bank.
IN_FLIGHT_STATUSES: Final[tuple[str, ...]] = ("scheduled", "in_transit")

#: Statuses that require a `payout_reversal` transaction.
REVERSED_STATUSES: Final[tuple[str, ...]] = ("failed", "returned")

#: Per-rail transfer ceiling in minor units. same_day_ach caps at $1,000,000 per transfer
#: and has a hard bank cutoff; the others are Payzeno risk limits, not scheme ones.
RAIL_LIMIT_MINOR: Final[dict[str, int]] = {
    "standard_ach": 25_000_000_00,
    "same_day_ach": 1_000_000_00,
    "sepa": 10_000_000_00,
    "faster_payments": 1_000_000_00,
    "debit_ach": 500_000_00,
}


class Payout(Base, TimestampMixin, LivemodeMixin):
    """One outbound (or, for ``debit_ach``, inbound) bank transfer."""

    __tablename__ = "payout"
    entity_name: ClassVar[str] = "payout"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: payzeno-api's `bank_account.id`. Resolved through `bank_account_projection` —
    #: there is no ledger→api route on the payout path.
    bank_account_id: Mapped[str] = mapped_column(Text, nullable=False)

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    status: Mapped[str] = mapped_column(
        payout_status_enum, nullable=False, server_default="scheduled"
    )
    method: Mapped[str] = mapped_column(payout_method_enum, nullable=False)

    #: Banking-calendar dates, not instants. `BankingCalendar.next_business_day` computes
    #: `available_on` from `settlement_date + merchant.payout_delay_days`.
    available_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    arrival_estimate: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    statement_descriptor: Mapped[str] = mapped_column(String(22), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(payout_failure_code_enum, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    ledger_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=True
    )
    reversal_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("ledger_transaction.id", ondelete="RESTRICT"), nullable=True
    )
    #: The rail's own reference, returned by `PayoutInitiator.initiate`.
    bank_reference: Mapped[str | None] = mapped_column(Text, nullable=True)

    initiated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        # Invariant 7. A payout in failed/returned MUST carry its reversal.
        CheckConstraint(
            "status not in ('failed','returned') or reversal_transaction_id is not null",
            name="reversal_present",
        ),
        Index(
            "ix_payout_merchant_created",
            "merchant_id",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
        Index("ix_payout_status_available", "status", "available_on"),
        # Unique from 0033. One in-flight payout per (merchant, currency, livemode).
        Index(
            "pix_payout_in_flight",
            "merchant_id",
            "currency",
            "livemode",
            unique=True,
            postgresql_where="status in ('scheduled','in_transit')",
        ),
    )

    def is_in_flight(self) -> bool:
        """Money has left the payable balance but has not landed."""
        return self.status in IN_FLIGHT_STATUSES

    def requires_reversal(self) -> bool:
        """Whether ``chk_payout_reversal_present`` applies to this row's status."""
        return self.status in REVERSED_STATUSES

    def exceeds_rail_limit(self) -> bool:
        """Whether the amount is over the rail's per-transfer ceiling.

        ``PayoutInitiator.initiate`` checks this and raises
        :class:`BankAccountUnusableError` rather than letting the rail reject the file
        hours later, when the money has already left ``merchant_payable``.
        """
        limit = RAIL_LIMIT_MINOR.get(self.method)
        return limit is not None and self.amount_minor > limit

    def is_merchant_debit(self) -> bool:
        """``debit_ach`` pulls funds *from* the merchant on a negative balance.

        Created by ``NegativeBalanceJob`` past the escalation threshold. It runs the same
        state machine and posts Dr ``cash`` / Cr ``merchant_payable``.
        """
        return self.method == "debit_ach"
