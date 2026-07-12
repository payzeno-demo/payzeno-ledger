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
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    country: Mapped[str | None] = mapped_column(Country, nullable=True)
    default_currency: Mapped[str | None] = mapped_column(Currency, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False)
    risk_tier: Mapped[str] = mapped_column(Text, nullable=False)
    reserve_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reserve_hold_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
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
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_merchant_projection_status", "status"),
        Index("ix_merchant_projection_occurred", "source_occurred_at"),
    )

    def payouts_allowed(self) -> bool:
        """``restricted`` may charge but may not receive payouts; ``suspended`` neither.

        ``PayoutService.create_payout`` raises :class:`PayoutBlockedError` when this is
        false.
        """
        return self.status == "active"

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
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    acquirer: Mapped[str] = mapped_column(acquirer_enum, nullable=False)

    network_transaction_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    processor_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_method: Mapped[str] = mapped_column(Text, nullable=False)
    capture_at_settlement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    reserve_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    platform_fee_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_fee_fixed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    authorized_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

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

    def needs_deferred_capture(self) -> bool:
        """Whether settlement must issue the physical capture to the acquirer.

        True only for the deferred-settlement MCCs (travel, lodging, car rental) whose
        merchants are configured ``capture_at_settlement``. Eleven merchants had it on
        during PAY-2041, and 218 of the 1,847 duplicated items belonged to them — those
        218 are the only ones that reached a cardholder.
        """
        return self.capture_at_settlement and self.captured_at is None


class BankAccountProjection(Base, LivemodeMixin, ProjectionOrderingMixin):
    """The ledger's copy of ``payzeno_api.bank_account``, fed by ``merchant.bank_account_verified``.

    Without it ``payout.bank_account_id`` is an id the ledger cannot resolve:
    ``BankAccount`` is owned by payzeno-api, ``PayoutInitiator.initiate`` has to produce
    an ACH/SEPA/FPS instruction, and there is no ledger→api route on the payout path.
    """

    __tablename__ = "bank_account_projection"
    entity_name: ClassVar[str] = "bank_account_projection"

    bank_account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    country: Mapped[str] = mapped_column(Country, nullable=False)
    scheme: Mapped[str] = mapped_column(Text, nullable=False)

    #: A vault token. Payzeno never holds the digits — arc PCI §11.3.
    account_number_token: Mapped[str] = mapped_column(Text, nullable=False)
    routing_last_four: Mapped[str | None] = mapped_column(LastFour, nullable=True)
    iban_last_four: Mapped[str | None] = mapped_column(LastFour, nullable=True)
    bic: Mapped[str | None] = mapped_column(String(11), nullable=True)
    sort_code_last_four: Mapped[str | None] = mapped_column(LastFour, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_bank_account_projection_merchant", "merchant_id", "currency"),
        # CreatePayoutRequest.bank_account_id is optional, so the ledger must be able to
        # find the merchant's default account for a currency. This index is how.
        Index(
            "pix_bank_account_projection_default",
            "merchant_id",
            "currency",
            "livemode",
            unique=True,
            postgresql_where="is_default",
        ),
    )

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
