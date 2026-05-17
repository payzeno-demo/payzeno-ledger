"""Circuit breakers and the ``ProcessorClient`` decorator that uses them.

`BreakerProcessorClient` is the object every service actually holds. It implements
``ProcessorClient`` itself and routes each call to the acquirer-specific client behind
a per-acquirer circuit. When the circuit is open the call raises
``ProcessorUnavailableError`` **without** an HTTP request — that is the whole point:
during an acquirer degradation the drain would otherwise re-attempt captures against a
processor that is already returning 504s.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import date
from typing import Awaitable, Callable, Deque, TypeVar

import redis.asyncio as aioredis

from app.config import Settings
from app.errors import ProcessorUnavailableError, UpstreamError
from app.logging import get_logger
from app.ports import CaptureResponse, CaptureStatus, CircuitBreaker, ProcessorClient

logger = get_logger(__name__)

T = TypeVar("T")


class InMemoryCircuitBreaker(CircuitBreaker):
    """Process-local breaker. Used by docker-compose and by the ops CLI.

    Not correct across four ECS tasks — each task carries its own opinion about the
    acquirer's health — which is exactly why production uses the Redis one.
    """

    def __init__(self, *, threshold_pct: int, window: int, reset_seconds: int) -> None:
        self._threshold_pct = threshold_pct
        self._window = window
        self._reset_seconds = reset_seconds
        self._outcomes: dict[str, Deque[bool]] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, name: str) -> bool:
        opened = self._opened_at.get(name)
        if opened is None:
            return False
        if time.monotonic() - opened >= self._reset_seconds:
            del self._opened_at[name]
            self._outcomes.pop(name, None)
            return False
        return True

    async def call(self, name: str, fn: Callable[[], Awaitable[T]]) -> T:
        if self.is_open(name):
            raise ProcessorUnavailableError(
                f"circuit open for {name}",
                code="processor_unavailable",
                circuit=name,
            )
        try:
            result = await fn()
        except UpstreamError:
            self._record(name, False)
            raise
        self._record(name, True)
        return result

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        threshold_pct: int,
        window: int,
        reset_seconds: int,
    ) -> None:
        self._redis = redis
        self._threshold_pct = threshold_pct
        self._window = window
        self._reset_seconds = reset_seconds
        self._open_cache: dict[str, tuple[float, bool]] = {}
        self._lock = asyncio.Lock()

    def _open_key(name: str) -> str:
        return f"payzeno:ledger:breaker:{name}:open"

    @staticmethod
    def __init__(
        self,
        *,
        clients: dict[str, ProcessorClient],
        breaker: CircuitBreaker,
        default_acquirer: str,
    ) -> None:
        self._clients = clients
        self._breaker = breaker
        self._default_acquirer = default_acquirer

    def _client_for(self, acquirer: str | None) -> ProcessorClient:
        key = acquirer or self._default_acquirer
        client = self._clients.get(key)
        if client is None:
            raise ProcessorUnavailableError(
                f"no processor client configured for {key}",
                code="processor_unavailable",
                acquirer=key,
            )
        return client

    async def confirm_settlement(
        self, acquirer: str, acquirer_reference: str, batch_id: str
    ) -> None:
        client = self._client_for(acquirer)
        await self._breaker.call(
            f"{acquirer}:confirm_settlement",
            lambda: client.confirm_settlement(
                acquirer=acquirer,
                acquirer_reference=acquirer_reference,
                batch_id=batch_id,
            ),
        )

    async def get_capture_status(self, acquirer: str, idempotency_key: str) -> CaptureStatus:
        client = self._client_for(acquirer)
        return await self._breaker.call(
            f"{acquirer}:get_capture_status",
            lambda: client.get_capture_status(
                acquirer=acquirer, idempotency_key=idempotency_key
            ),
        )

