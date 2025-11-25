"""Projections of payzeno-api entities. Read-optimised, eventually consistent, never authoritative.

`domain-model.md` §0.5: exactly one service **owns** each entity. payzeno-api owns
``merchant``, ``charge`` and ``bank_account``; these three tables are the ledger's copies,
fed off the bus and written only by ``MerchantEventConsumer`` and ``PaymentEventConsumer``.

Every write is a **conditional upsert** guarded on ``source_occurred_at``::

    ... ON CONFLICT (...) DO UPDATE SET ...
        WHERE <table>.source_occurred_at < EXCLUDED.source_occurred_at

Ordering is not guaranteed on the bus and ``source_event_id`` is a ULID of the *event*,
not a monotonic token for the entity. Without the timestamp guard a stale
``merchant.status_changed`` silently un-restricts a suspended merchant, or re-enables
``capture_at_settlement`` in the ledger's view. Migration ``0031``.

**arc PCI.** Nothing here holds a PAN, a full account number, or anything a
``RedactingFormatter`` would have to catch. ``account_number_token`` is a vault token and
the last-four fields are display fragments.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Country,
    Currency,
    LastFour,
    LivemodeMixin,
    ProjectionOrderingMixin,
    acquirer_enum,
)


class MerchantProjection(Base, LivemodeMixin, ProjectionOrderingMixin):
    """The ledger's copy of ``payzeno_api.merchant``.

    Fed by ``merchant.created`` (which carries every NOT NULL column below),
    ``merchant.updated`` (the full mutable field set — the **only** way
    ``capture_at_settlement`` ever becomes true in the ledger) and
    ``merchant.status_changed`` (status only).
    """

    __tablename__ = "merchant_projection"
    merchant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    country: Mapped[str | None] = mapped_column(Country, nullable=True)
    platform_fee_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_fee_fixed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout_delay_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    #: A COLUMN, not a feature flag. SettlementPoster nevertheless reads
    #: capture_at_settlement off the CHARGE row, never off this one — see
    #: SettlementCharge below.
    capture_at_settlement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    __table_args__ = (
        Index("ix_merchant_projection_status", "status"),
        Index("ix_merchant_projection_occurred", "source_occurred_at"),
    )

    def is_interchange_plus(self) -> bool:
        """Whether ``apportion_fee`` applies. Blended merchants never touch it."""
        return self.pricing_model == "interchange_plus"


class SettlementCharge(Base, LivemodeMixin, ProjectionOrderingMixin):
    """The ledger's copy of ``payzeno_api.charge``, written from ``PaymentAuthorizedPayload``.

    ``reserve_bps``, ``platform_fee_bps``, ``platform_fee_fixed_minor`` and
    ``capture_at_settlement`` are **denormalised at authorisation time** so a later
    merchant change cannot retroactively alter an in-flight settlement. That reasoning
    applies with far higher stakes to ``capture_at_settlement`` than to ``reserve_bps``:
    flipping the merchant flag mid-flight would otherwise decide whether an already
    authorised charge gets a second cardholder capture.

    ``SettlementPoster`` therefore reads ``charge.capture_at_settlement`` off **this**
    row, never off ``merchant_projection``.
    """

    network_transaction_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reserve_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    scheme: Mapped[str] = mapped_column(Text, nullable=False)

    def matches_rail(self, method: str) -> bool:
        """Whether this account's scheme can carry the given payout method."""
        required = {
            "standard_ach": "aba",
            "same_day_ach": "aba",
            "debit_ach": "aba",
            "sepa": "iban",
            "faster_payments": "uk_sort_code",
        }.get(method)
        return required is not None and self.scheme == required
