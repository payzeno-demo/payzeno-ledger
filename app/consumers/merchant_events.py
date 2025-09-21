"""``payzeno-ledger-merchants`` — the merchant topic consumer.

Four event types, all of them projection writes. This consumer is the only reason the
ledger knows a merchant's settlement tolerance, reserve rate, payout schedule or
verified bank account exists.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from payzeno_contracts.events import EventEnvelope
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.consumers.base import BaseConsumer, supported_types
from app.consumers.handlers.merchants import (
    handle_bank_account_verified,
    handle_merchant_created,
    handle_merchant_status_changed,
    handle_merchant_updated,
)
from app.logging import get_logger
from app.ports import Clock, SessionFactory
from app.repositories.processed_event import ProcessedEventRepository
from app.repositories.projections import (
    BankAccountProjectionRepository,
    MerchantProjectionRepository,
)
from app.services.accounts import AccountResolver

logger = get_logger(__name__)


class MerchantEventConsumer(BaseConsumer):
    """Keeps ``merchant_projection`` and ``bank_account_projection`` current."""

    consumer_name: ClassVar[str] = "merchant_events"
    handled_types: ClassVar[frozenset[str]] = supported_types(
        "merchant.created",
        "merchant.updated",
        "merchant.status_changed",
        "merchant.bank_account_verified",
    )

    def __init__(
        self,
        sessions: SessionFactory,
        processed: ProcessedEventRepository,
        sqs_client_factory: Any,
        settings: Settings,
        merchants: MerchantProjectionRepository,
        banks: BankAccountProjectionRepository,
        resolver: AccountResolver,
        clock: Clock,
    ) -> None:
        super().__init__(sessions, processed, sqs_client_factory)
        self.queue_url = settings.sqs_merchants_queue_url
        self._merchants = merchants
        self._banks = banks
        self._resolver = resolver
        self._clock = clock

    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        payload = dict(event.payload)
        occurred_at = _parse(event.occurred_at)

        if event.type == "merchant.created":
            await handle_merchant_created(
                session,
                payload,
                merchants=self._merchants,
                resolver=self._resolver,
                clock=self._clock,
                event_id=event.id,
                occurred_at=occurred_at,
                livemode=event.livemode,
            )
        elif event.type == "merchant.updated":
            await handle_merchant_updated(
                session,
                payload,
                merchants=self._merchants,
                clock=self._clock,
                event_id=event.id,
                occurred_at=occurred_at,
                livemode=event.livemode,
            )
        elif event.type == "merchant.status_changed":
            await handle_merchant_status_changed(
                session,
                payload,
                merchants=self._merchants,
                clock=self._clock,
                event_id=event.id,
                occurred_at=occurred_at,
            )
        elif event.type == "merchant.bank_account_verified":
            await handle_bank_account_verified(
                session,
                payload,
                banks=self._banks,
                clock=self._clock,
                event_id=event.id,
                occurred_at=occurred_at,
                livemode=event.livemode,
            )
        else:  # pragma: no cover - handled_types already filtered
            logger.warning("merchant_consumer_unrouted", event_type=event.type)


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
