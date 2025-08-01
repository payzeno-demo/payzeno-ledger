"""Event publishing.

Two implementations of ``app.ports.EventPublisher`` with strictly separated jobs:

* :class:`~app.publishers.outbox.OutboxPublisher` — every business path. Writes into
  ``event_outbox`` inside the caller's transaction.
* :class:`~app.publishers.sns.SnsPublisher` — drain only. One caller, ever:
  ``app/workers/outbox_drain.py``.

Both stamp ``envelope.version`` from ``payzeno_contracts.events.EVENT_SCHEMA_VERSIONS``.
"""

from app.publishers.envelope import TOPIC_BY_EVENT_TYPE, build_envelope, topic_for
from app.publishers.outbox import OutboxPublisher
from app.publishers.sns import SnsPublisher

__all__ = [
    "TOPIC_BY_EVENT_TYPE",
    "OutboxPublisher",
    "SnsPublisher",
    "build_envelope",
    "topic_for",
]
