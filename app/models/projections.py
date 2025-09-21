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
    platform_fee_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    #: A COLUMN, not a feature flag. SettlementPoster nevertheless reads
    #: capture_at_settlement off the CHARGE row, never off this one — see
    #: SettlementCharge below.
    capture_at_settlement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    network_transaction_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reserve_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    scheme: Mapped[str] = mapped_column(Text, nullable=False)

