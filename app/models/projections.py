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
    entity_name: ClassVar[str] = "merchant_projection"

    merchant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    country: Mapped[str | None] = mapped_column(Country, nullable=True)
    default_currency: Mapped[str | None] = mapped_column(Currency, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False)
    risk_tier: Mapped[str] = mapped_column(Text, nullable=False)
    reserve_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pricing_model: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="blended"
    )
    platform_fee_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_fee_fixed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout_delay_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    #: Variance tolerance per settlement item. SettlementPoster compares
    #: abs(item.variance_minor) against this and raises
    #: SettlementVarianceExceededError above it.
    settlement_tolerance_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="100"
    )
    #: A COLUMN, not a feature flag. SettlementPoster nevertheless reads
    #: capture_at_settlement off the CHARGE row, never off this one — see
    #: SettlementCharge below.
    capture_at_settlement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    payout_schedule: Mapped[str] = mapped_column(Text, nullable=False)
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

    __tablename__ = "settlement_charge"
    entity_name: ClassVar[str] = "settlement_charge"

    charge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(Text, nullable=False)
    acquirer: Mapped[str] = mapped_column(acquirer_enum, nullable=False)

    network_transaction_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_method: Mapped[str] = mapped_column(Text, nullable=False)
    reserve_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    authorized_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __table_args__ = (
        Index("ix_settlement_charge_merchant", "merchant_id"),
        # Match strategy 2 (NetworkTransactionMatch) drives off this.
        Index(
            "uq_settlement_charge_network_txn",
            "acquirer",
            "network_transaction_id",
            unique=True,
        ),
        # The PRIMARY reconciliation match key — strategy 1, ExactReferenceMatch.
        Index(
            "ix_settlement_charge_processor_reference", "acquirer", "processor_reference"
        ),
        # HeuristicAmountWindowMatch scans this: same (merchant, amount, currency) with
        # authorized_at inside ±48h, and exactly one candidate.
        Index(
            "ix_settlement_charge_heuristic",
            "merchant_id",
            "amount_minor",
            "currency",
            "authorized_at",
        ),
    )

    def is_captured(self) -> bool:
        """Whether payzeno-api has already captured this charge itself."""
        return self.captured_at is not None

    __tablename__ = "bank_account_projection"
    bank_account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    country: Mapped[str] = mapped_column(Country, nullable=False)
    scheme: Mapped[str] = mapped_column(Text, nullable=False)

    routing_last_four: Mapped[str | None] = mapped_column(LastFour, nullable=True)
    iban_last_four: Mapped[str | None] = mapped_column(LastFour, nullable=True)
    sort_code_last_four: Mapped[str | None] = mapped_column(LastFour, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False)
    def is_usable(self) -> bool:
        """Only a ``validated`` account may receive money.

        ``PayoutInitiator.initiate`` raises :class:`BankAccountUnusableError` otherwise —
        including for a scheme that does not match the rail (``sepa`` needs ``iban``,
        ``faster_payments`` needs ``uk_sort_code``, both ACH rails need ``aba``).
        """
        return self.status == "validated"

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
