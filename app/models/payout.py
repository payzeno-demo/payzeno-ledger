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
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    status: Mapped[str] = mapped_column(
        payout_status_enum, nullable=False, server_default="scheduled"
    )
    arrival_estimate: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    #: The rail's own reference, returned by `PayoutInitiator.initiate`.
    bank_reference: Mapped[str | None] = mapped_column(Text, nullable=True)

    initiated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
