"""Outbound acquirer clients.

Everything above ``app/clients`` depends on ``app.ports.ProcessorClient``, never on a
concrete acquirer. The only place a concrete client name appears outside this package
is ``app/container.py``, which builds the routing table for
:class:`~app.clients.breaker.BreakerProcessorClient`.
"""

from app.clients.breaker import (
    BreakerProcessorClient,
    InMemoryCircuitBreaker,
    RedisCircuitBreaker,
    build_breaker,
)
from app.clients.http import LedgerHttpxClient
from app.clients.nordpay import NordpayClient
from app.clients.sandbox import SandboxProcessorClient
from app.clients.worldflow import WorldflowClient

__all__ = [
    "BreakerProcessorClient",
    "InMemoryCircuitBreaker",
    "LedgerHttpxClient",
    "NordpayClient",
    "RedisCircuitBreaker",
    "SandboxProcessorClient",
    "WorldflowClient",
    "build_breaker",
]
