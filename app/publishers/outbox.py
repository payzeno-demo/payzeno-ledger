"""Transactional outbox publisher — the ``EventPublisher`` every business path uses.

The publish is an INSERT into ``event_outbox`` inside the caller's transaction. If that
transaction rolls back, the event rolls back with it. That property is not decoration:
``SettlementPoster`` publishes ``settlement.item_settled`` before its transaction
commits, and during an acquirer degradation a direct-to-SNS publisher would emit
thousands of phantom settlement events for items that never settled.

``OutboxDrainJob`` is the only reader; it hands rows to :class:`SnsPublisher`.

The session is a **per-call keyword argument**, never an instance attribute. One
``OutboxPublisher`` is constructed in ``app/container.py`` and shared by the sweep, the
retry drain, both consumers and every API route — all of which are running concurrently
on different connections. An instance-bound session would put the sweep's events in the
drain's transaction.
"""

from __future__ import annotations

import json
from typing import Any

from payzeno_contracts.events import EVENT_SCHEMA_VERSIONS
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models.events import EventOutbox
from app.ports import EventPublisher
from app.publishers.envelope import build_envelope, topic_for
from app.repositories.outbox import EventOutboxRepository

logger = get_logger(__name__)


class OutboxPublisher(EventPublisher):
    """Stages events in ``event_outbox`` inside the caller's transaction."""

    def __init__(self, outbox: EventOutboxRepository) -> None:
        self._outbox = outbox

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        merchant_id: str | None,
        correlation_id: str,
        causation_id: str | None = None,
        session: AsyncSession | None = None,
        livemode: bool = True,
    ) -> str:
        """Stage one event and return the envelope id.

        ``session`` is keyword-optional only so the signature still satisfies
        ``app.ports.EventPublisher``. A publish with no session is a programming error
        and raises immediately — silently writing outside the business transaction is
        the failure mode this class exists to prevent.
        """
        if session is None:
            raise RuntimeError(
                "OutboxPublisher.publish requires session= — an outbox event must live "
                "or die with the caller's transaction"
            )

        envelope = build_envelope(
            event_type,
            payload,
            merchant_id=merchant_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            livemode=livemode,
            version=EVENT_SCHEMA_VERSIONS.get(event_type, 1),
        )
        row = EventOutbox(
            id=envelope.id,
            event_type=envelope.type,
            topic=topic_for(event_type),
            payload=_as_dict(envelope),
            merchant_id=merchant_id,
            correlation_id=envelope.correlation_id,
            livemode=livemode,
            status="pending",
            attempt_count=0,
        )
        await self._outbox.add(session, row)
        logger.debug(
            "outbox_staged",
            event_id=envelope.id,
            event_type=event_type,
            merchant_id=merchant_id,
        )
        return envelope.id

    async def publish_many(
        self,
        session: AsyncSession,
        events: list[tuple[str, dict[str, Any]]],
        *,
        merchant_id: str | None,
        correlation_id: str,
        livemode: bool = True,
    ) -> list[str]:
        """Stage several events in one transaction.

        Used by ``ReconciliationService._publish_completion``, which chunks
        ``settlement.completed`` at 1000 charge ids per event and must not leave half
        the chunks staged if the run row fails to update.
        """
        ids: list[str] = []
        for event_type, payload in events:
            ids.append(
                await self.publish(
                    event_type,
                    payload,
                    merchant_id=merchant_id,
                    correlation_id=correlation_id,
                    session=session,
                    livemode=livemode,
                )
            )
        return ids


def _as_dict(envelope: Any) -> dict[str, Any]:
    """Serialise a contracts envelope regardless of its runtime representation."""
    dumper = getattr(envelope, "model_dump", None)
    if callable(dumper):
        return dict(dumper(mode="json"))
    return json.loads(json.dumps(envelope, default=str))
