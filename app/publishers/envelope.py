"""Builds the one envelope shape every Payzeno event crosses the bus in.

There is no local ``EventEnvelope`` class. The dataclass comes from
``payzeno_contracts.events`` and this module is the only place in payzeno-ledger that
constructs one — a second construction site is a second opinion about
``correlation_id`` propagation, and ``events.ts`` is explicit that the value must come
from the originating HTTP request where one exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from payzeno_contracts.events import EVENT_TOPICS, EventEnvelope

from app.domain.ids import new_id
from app.middleware.correlation_id import current_correlation_id

SOURCE = "payzeno-ledger"

#: Reverse index of EVENT_TOPICS: event type -> SNS topic name. Built once at import.
TOPIC_BY_EVENT_TYPE: dict[str, str] = {
    event_type: topic
    for topic, event_types in EVENT_TOPICS.items()
    for event_type in event_types
}


def topic_for(event_type: str) -> str:
    """Return the SNS topic an event type belongs on.

    Raises ``KeyError`` deliberately: an event type with no topic is a contract drift
    between payzeno-ledger and payzeno-contracts and must fail loudly at the publish
    site rather than silently vanish.
    """
    return TOPIC_BY_EVENT_TYPE[event_type]


def build_envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    merchant_id: str | None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    livemode: bool = True,
    occurred_at: datetime | None = None,
    version: int = 1,
) -> EventEnvelope:
    """Wrap a payload in the frozen envelope.

    ``occurred_at`` is when the fact became true in the ledger — the caller's clock —
    not when the row is drained out of the outbox. A drain that stamps its own time
    would make every consumer's ordering guard useless.
    """
    resolved_correlation = correlation_id or current_correlation_id() or new_id("cor")
    stamped_at = occurred_at or datetime.now(tz=timezone.utc)
    return EventEnvelope(
        id=new_id("evt"),
        type=event_type,
        version=version,
        occurred_at=stamped_at.isoformat(),
        source=SOURCE,
        correlation_id=resolved_correlation,
        causation_id=causation_id,
        merchant_id=merchant_id,
        livemode=livemode,
        payload=payload,
    )
