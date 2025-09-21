"""Consumer base — long-poll loop plus the dedupe claim.

Delivery is at-least-once and ordering is not guaranteed, so every consumer must be
idempotent on ``envelope.id`` and must tolerate an older event arriving after a newer
one. Both properties are handled here rather than by each handler:

* idempotence — :meth:`BaseConsumer._claim_event`, an INSERT-first claim on
  ``processed_event`` in the **same transaction** as the handler's side effects
* ordering — projection writes are conditional upserts guarded on
  ``source_occurred_at``; see ``app/consumers/handlers/``

There is deliberately no ``_already_processed`` / ``_mark_processed`` pair. Check-then-act
around a side effect is exactly the shape of PAY-2041 and ADR 0011 forbids it in the
money path — and this is the money path.
"""

from __future__ import annotations

import abc
import asyncio
import json
from typing import Any, ClassVar

from payzeno_contracts.events import EventEnvelope, EventType, PayzenoEvent
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import PayzenoLedgerError
from app.logging import get_logger
from app.metrics import metrics
from app.ports import SessionFactory
from app.repositories.processed_event import ProcessedEventRepository

logger = get_logger(__name__)

#: SQS long-poll wait. Twenty seconds is the service maximum and the cheapest setting.
LONG_POLL_SECONDS = 20
MAX_MESSAGES_PER_RECEIVE = 10


class BaseConsumer(abc.ABC):
    """One SQS queue, one transaction per message."""

    queue_url: ClassVar[str] = ""
    handled_types: ClassVar[frozenset[str]] = frozenset()
    consumer_name: ClassVar[str] = "base"

    def __init__(
        self,
        sessions: SessionFactory,
        processed: ProcessedEventRepository,
        sqs_client_factory: Any,
    ) -> None:
        self._sessions = sessions
        self._processed = processed
        self._sqs_client_factory = sqs_client_factory

    @abc.abstractmethod
    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        """Apply one event's side effects inside the caller's transaction."""

    async def run(self, stop: asyncio.Event) -> None:
        """Long-poll until ``stop`` is set.

        One transaction per message: the claim and the handler commit together, so a
        crash between them replays the message rather than losing it.
        """
        logger.info(
            "consumer_started", consumer=self.consumer_name, queue_url=self.queue_url
        )
        async with self._sqs_client_factory() as sqs:
            while not stop.is_set():
                response = await sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=MAX_MESSAGES_PER_RECEIVE,
                    WaitTimeSeconds=LONG_POLL_SECONDS,
                    MessageAttributeNames=["All"],
                )
                for message in response.get("Messages", []):
                    handled = await self._handle_message(message)
                    if handled:
                        await sqs.delete_message(
                            QueueUrl=self.queue_url,
                            ReceiptHandle=message["ReceiptHandle"],
                        )
        logger.info("consumer_stopped", consumer=self.consumer_name)

    async def _handle_message(self, message: dict[str, Any]) -> bool:
        """Return True when the message may be deleted from the queue."""
        try:
            envelope = self._parse(message)
        except (ValueError, KeyError) as exc:
            # Unparseable body. Leave it to the redrive policy; deleting it here would
            # destroy the only copy of a message we could not read.
            logger.error(
                "consumer_unparseable_message",
                consumer=self.consumer_name,
                error=str(exc),
            )
            return False

        if envelope.type not in self.handled_types:
            # Subscribed by topic, filtered by type. Not ours, but it is on our queue,
            # so it will never be anyone else's either — delete it.
            return True

        try:
            async with self._sessions.begin() as session:
                claimed = await self._claim_event(session, envelope.id)
                if not claimed:
                    metrics.increment(
                        "ConsumerEventDeduped", consumer=self.consumer_name
                    )
                    return True
                await self.handle(session, envelope)
        except PayzenoLedgerError as exc:
            logger.error(
                "consumer_handler_failed",
                consumer=self.consumer_name,
                event_id=envelope.id,
                event_type=envelope.type,
                code=exc.code,
            )
            metrics.increment(
                "ConsumerEventFailed", consumer=self.consumer_name, code=exc.code
            )
            return False

        metrics.increment(
            "ConsumerEventHandled",
            consumer=self.consumer_name,
            event_type=envelope.type,
        )
        return True

    async def _claim_event(self, session: AsyncSession, event_id: str) -> bool:
        """``INSERT INTO processed_event ... ON CONFLICT DO NOTHING RETURNING event_id``.

        Returns False when the row already existed — the handler is then not called at
        all. Runs in the same transaction as :meth:`handle`, so a rollback releases the
        claim along with the side effects.
        """
        return await self._processed.claim(
            session, event_id=event_id, consumer=self.consumer_name
        )

    def _parse(self, message: dict[str, Any]) -> EventEnvelope:
        """Unwrap the SNS notification and validate against the contracts envelope."""
        raw_body = json.loads(message["Body"])
        # SNS-to-SQS delivery nests the publisher's payload under "Message".
        payload = json.loads(raw_body["Message"]) if "Message" in raw_body else raw_body
        return _to_envelope(payload)


def _to_envelope(payload: dict[str, Any]) -> EventEnvelope:
    validator = getattr(PayzenoEvent, "model_validate", None)
    if callable(validator):
        return validator(payload)
    return EventEnvelope(**payload)


def supported_types(*types: str) -> frozenset[str]:
    """Small helper so subclasses declare ``handled_types`` readably.

    Values are validated against the contracts ``EventType`` union at import time, so a
    typo is an ImportError rather than a queue that silently drops everything.
    """
    known = set(getattr(EventType, "__args__", ()) or ())
    if known:
        unknown = [value for value in types if value not in known]
        if unknown:
            raise ValueError(f"unknown event types: {unknown}")
    return frozenset(types)
