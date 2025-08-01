"""Inbound bus consumers.

Two queues, both fed by SNS fanout from payzeno-api:

===============================  ==========================  ===========================
Queue                            Class                        Topic
===============================  ==========================  ===========================
``payzeno-ledger-payments``      :class:`PaymentEventConsumer` ``payzeno-payments-events``
``payzeno-ledger-merchants``     :class:`MerchantEventConsumer` ``payzeno-merchant-events``
===============================  ==========================  ===========================

``register_consumers`` is called from ``app/main.py``'s lifespan; the supervisor owns the
tasks so a shutdown drains in-flight messages instead of dropping them.
"""

from __future__ import annotations

from app.consumers.base import BaseConsumer
from app.consumers.merchant_events import MerchantEventConsumer
from app.consumers.payment_events import PaymentEventConsumer
from app.consumers.sqs import ConsumerSupervisor, sqs_client_factory

__all__ = [
    "BaseConsumer",
    "ConsumerSupervisor",
    "MerchantEventConsumer",
    "PaymentEventConsumer",
    "register_consumers",
    "sqs_client_factory",
]


def register_consumers(container: object) -> ConsumerSupervisor:
    """Build the supervisor over every consumer the container constructed.

    The container is the only place consumers are constructed; this function only
    decides which of them run in this process.
    """
    consumers: list[BaseConsumer] = [
        getattr(container, "payment_event_consumer"),
        getattr(container, "merchant_event_consumer"),
    ]
    return ConsumerSupervisor(consumers)
