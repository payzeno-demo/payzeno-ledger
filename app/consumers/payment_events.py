"""``payzeno-ledger-payments`` — the payments topic consumer.

Subscribed to ``payzeno-payments-events``; six of that topic's ten types are ours. The
rest arrive on the queue (the subscription is not filtered) and are deleted unhandled by
:meth:`BaseConsumer._handle_message`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from payzeno_contracts.events import EventEnvelope
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.consumers.base import BaseConsumer, supported_types
from app.consumers.handlers.payments import (
    handle_dispute_closed,
    handle_dispute_opened,
    handle_payment_authorized,
    handle_payment_canceled,
    handle_payment_captured,
    handle_refund_created,
)
from app.logging import get_logger
from app.ports import Clock, SessionFactory
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.repositories.processed_event import ProcessedEventRepository
from app.repositories.projections import SettlementChargeRepository
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)


class PaymentEventConsumer(BaseConsumer):
    """Projects charges and posts the auth/capture/refund/dispute side of the ledger."""

    consumer_name: ClassVar[str] = "payment_events"
    handled_types: ClassVar[frozenset[str]] = supported_types(
        "payment.authorized",
        "payment.captured",
        "payment.canceled",
        "refund.created",
        "dispute.opened",
        "dispute.closed",
    )

    def __init__(
        self,
        sessions: SessionFactory,
        processed: ProcessedEventRepository,
        sqs_client_factory: Any,
        settings: Settings,
        charges: SettlementChargeRepository,
        transactions: LedgerTransactionRepository,
        ledger: LedgerPoster,
        clock: Clock,
    ) -> None:
        super().__init__(sessions, processed, sqs_client_factory)
        self.queue_url = settings.sqs_payments_queue_url
        self._charges = charges
        self._transactions = transactions
        self._ledger = ledger
        self._clock = clock

    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        payload = dict(event.payload)
        occurred_at = _parse(event.occurred_at)
        common = {
            "event_id": event.id,
            "livemode": event.livemode,
        }

        if event.type == "payment.authorized":
            await handle_payment_authorized(
                session,
                payload,
                charges=self._charges,
                ledger=self._ledger,
                clock=self._clock,
                occurred_at=occurred_at,
                **common,
            )
        elif event.type == "payment.captured":
            await handle_payment_captured(
                session,
                payload,
                charges=self._charges,
                ledger=self._ledger,
                clock=self._clock,
                occurred_at=occurred_at,
                **common,
            )
        elif event.type == "payment.canceled":
            await handle_payment_canceled(
                session, payload, ledger=self._ledger, **common
            )
        elif event.type == "refund.created":
            await handle_refund_created(
                session, payload, ledger=self._ledger, **common
            )
        elif event.type == "dispute.opened":
            await handle_dispute_opened(
                session,
                payload,
                ledger=self._ledger,
                transactions=self._transactions,
                **common,
            )
        elif event.type == "dispute.closed":
            await handle_dispute_closed(
                session, payload, ledger=self._ledger, **common
            )
        else:  # pragma: no cover - handled_types already filtered
            logger.warning("payment_consumer_unrouted", event_type=event.type)


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
