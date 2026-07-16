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

    def _record(self, name: str, ok: bool) -> None:
        bucket = self._outcomes.setdefault(name, deque(maxlen=self._window))
        bucket.append(ok)
        if len(bucket) < self._window:
            return
        failures = sum(1 for entry in bucket if not entry)
        if failures * 100 // len(bucket) >= self._threshold_pct:
            self._opened_at[name] = time.monotonic()
            logger.warning("circuit_opened", circuit=name, failure_count=failures)


class RedisCircuitBreaker(CircuitBreaker):
    """Shared breaker state so all four ledger tasks agree the acquirer is down.

    State is two keys per circuit: a rolling list of outcomes and an ``open`` marker
    with a TTL equal to the reset window. Reads are cached in-process for 250ms so a
    200-item drain pass does not issue 200 round trips to Redis.
    """

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

    def is_open(self, name: str) -> bool:
        cached = self._open_cache.get(name)
        if cached is None:
            return False
        cached_at, value = cached
        if time.monotonic() - cached_at > 0.25:
            return False
        return value

    async def call(self, name: str, fn: Callable[[], Awaitable[T]]) -> T:
        if await self._is_open_remote(name):
            raise ProcessorUnavailableError(
                f"circuit open for {name}",
                code="processor_unavailable",
                circuit=name,
            )
        try:
            result = await fn()
        except UpstreamError:
            await self._record(name, ok=False)
            raise
        await self._record(name, ok=True)
        return result

    async def _is_open_remote(self, name: str) -> bool:
        value = await self._redis.get(self._open_key(name))
        is_open = value is not None
        self._open_cache[name] = (time.monotonic(), is_open)
        return is_open

    async def _record(self, name: str, *, ok: bool) -> None:
        key = self._outcome_key(name)
        async with self._lock:
            pipe = self._redis.pipeline()
            pipe.lpush(key, "1" if ok else "0")
            pipe.ltrim(key, 0, self._window - 1)
            pipe.lrange(key, 0, self._window - 1)
            _, _, outcomes = await pipe.execute()
        if len(outcomes) < self._window:
            return
        failures = sum(1 for entry in outcomes if entry in (b"0", "0"))
        if failures * 100 // len(outcomes) >= self._threshold_pct:
            await self._redis.set(self._open_key(name), "1", ex=self._reset_seconds)
            self._open_cache[name] = (time.monotonic(), True)
            logger.warning("circuit_opened", circuit=name, failure_count=failures)

    @staticmethod
    def _open_key(name: str) -> str:
        return f"payzeno:ledger:breaker:{name}:open"

    @staticmethod
    def _outcome_key(name: str) -> str:
        return f"payzeno:ledger:breaker:{name}:outcomes"


class BreakerProcessorClient(ProcessorClient):
    """`ProcessorClient` facade over the two acquirer clients plus the sandbox.

    Routing is by the ``acquirer`` value carried on the settlement line, so a batch
    imported from Nordpay never confirms against Worldflow. ``capture_deferred`` has no
    acquirer argument — the charge projection's acquirer is not on the call signature —
    so it uses the configured default acquirer for the charge's own client, which
    ``SettlementPoster`` selects by passing the item's acquirer through the reference.
    """

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

    async def capture_deferred(
        self,
        charge_id: str,
        amount_minor: int,
        currency: str,
        reference: str,
        *,
        idempotency_key: str,
        acquirer: str | None = None,
    ) -> CaptureResponse:
        client = self._client_for(acquirer)
        name = f"{acquirer or self._default_acquirer}:capture_deferred"
        return await self._breaker.call(
            name,
            lambda: client.capture_deferred(
                charge_id=charge_id,
                amount_minor=amount_minor,
                currency=currency,
                reference=reference,
                idempotency_key=idempotency_key,
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

    async def fetch_settlement_file(self, acquirer: str, processing_date: date) -> bytes:
        client = self._client_for(acquirer)
        return await self._breaker.call(
            f"{acquirer}:fetch_settlement_file",
            lambda: client.fetch_settlement_file(
                acquirer=acquirer, processing_date=processing_date
            ),
        )


def build_breaker(settings: Settings, redis: aioredis.Redis | None) -> CircuitBreaker:
    """Pick the breaker implementation. Redis in production, in-memory in compose."""
    if redis is None:
        return InMemoryCircuitBreaker(
            threshold_pct=settings.worldflow_breaker_threshold_pct,
            window=settings.worldflow_breaker_window,
            reset_seconds=settings.worldflow_breaker_reset_seconds,
        )
    return RedisCircuitBreaker(
        redis,
        threshold_pct=settings.worldflow_breaker_threshold_pct,
        window=settings.worldflow_breaker_window,
        reset_seconds=settings.worldflow_breaker_reset_seconds,
    )
