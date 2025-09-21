"""SQS client construction and the consumer supervisor.

Kept out of ``base.py`` so the consumer logic can be exercised against any object that
behaves like an aioboto3 SQS client — which is what the consumer tests do, and what the
compose environment does against LocalStack.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import aioboto3

from app.config import Settings
from app.logging import get_logger

logger = get_logger(__name__)

#: How long the supervisor waits for a consumer task to wind down before cancelling it.
SHUTDOWN_GRACE_SECONDS = 25.0


def sqs_client_factory(settings: Settings) -> Callable[[], Any]:
    """Return a zero-argument async context manager producing an SQS client.

    A factory rather than a client, because aioboto3 clients are context managers with a
    lifetime and each consumer owns its own for the duration of its poll loop.
    """
    aws_session = aioboto3.Session()

    @asynccontextmanager
    async def _factory() -> AsyncIterator[Any]:
        async with aws_session.client(
            "sqs",
            region_name=settings.aws_region,
            endpoint_url=settings.aws_endpoint_url or None,
        ) as client:
            yield client

    return _factory


class ConsumerSupervisor:
    """Runs every consumer as a task and shuts them down together.

    The ledger runs its consumers in the same process as the HTTP app. That is a
    deliberate trade: a separate deployment would double the task count for two queues
    whose combined throughput is well under one core, and the shared connection pool is
    sized for it (``DATABASE_POOL_SIZE``).
    """

    def __init__(self, consumers: list[Any]) -> None:
        self._consumers = consumers
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._stop.clear()
        for consumer in self._consumers:
            task = asyncio.create_task(
                consumer.run(self._stop), name=f"consumer:{consumer.consumer_name}"
            )
            self._tasks.append(task)
        logger.info("consumer_supervisor_started", consumers=len(self._tasks))

    async def stop(self) -> None:
        self._stop.set()
        if not self._tasks:
            return
        done, pending = await asyncio.wait(
            self._tasks, timeout=SHUTDOWN_GRACE_SECONDS
        )
        for task in pending:
            logger.warning("consumer_task_cancelled", task=task.get_name())
            task.cancel()
        for task in done:
            exception = task.exception()
            if exception is not None:
                logger.error(
                    "consumer_task_failed",
                    task=task.get_name(),
                    error=str(exception),
                )
        self._tasks.clear()
        logger.info("consumer_supervisor_stopped")
