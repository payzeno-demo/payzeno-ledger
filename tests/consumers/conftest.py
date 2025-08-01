"""Fixtures for the SQS consumer layer.

The property every test in this directory is really about is the one from ADR 0011:

    A dedupe guard around a side effect is a single atomic write or it is nothing.
    INSERT ... ON CONFLICT DO NOTHING RETURNING, in the same transaction as the effect.

`BaseConsumer._claim_event` and the handler it dispatches to run inside **one**
`sessions.begin()`. If the handler raises, the claim rolls back with it and the message
comes back on the queue to be processed once, properly, later. If the claim were committed
separately — check-then-act, the PAY-2041 shape — a handler failure would leave the event
marked processed and the projection unwritten, and nothing downstream would ever notice.

So the session factory here is deliberately a **shared-session** one: a single
:class:`ClaimSession` that every `begin()` hands back, with `committed` / `rolled_back`
flags on it. That is what lets a test assert "the claim and the side effect shared a fate"
without a real database. `tests/consumers/test_base.py` reads
`sessions_factory.session.rolled_back` for exactly that, and several tests call
`consumer.handle(sessions_factory.session, envelope)` directly, one layer below `run`.

The real concurrency property — two consumers on two tasks receiving the same message —
is not testable here and is not pretended to be. It lives in the `processed_event`
primary key `(event_id, consumer)` and in `tests/repositories/test_processed_event.py`
against real Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import pytest

from app.ports import SessionFactory

#: Every consumer fixture that needs a timestamp uses this one. It is inside the incident
#: window on purpose — several handler tests assert ordering against `source_occurred_at`.
NOW = datetime(2026, 4, 16, 11, 0, tzinfo=UTC)


class ClaimSession:
    """The one session a consumer pass gets.

    Records what the claim executed so a test can prove the INSERT happened before the
    handler's first write, and exposes the transaction verbs so a test can prove the two
    ended together.
    """

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append(str(statement))
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    @property
    def claim_count(self) -> int:
        """How many statements this session executed.

        The claim is one of them, and it is the first — a handler that wrote before the
        claim would show up here as an `added` entry with an empty `executed` list.
        """
        return len(self.executed)


class ConsumerSessionFactory(SessionFactory):
    """One session, handed to every caller. The explicit base is the `IMPLEMENTS` edge.

    `begin()` commits on a clean exit and rolls back on an exception, mirroring
    `PooledSessionFactory`. It does **not** reset the session between passes: a test that
    runs two messages through one consumer sees the accumulated record, which is how the
    dedupe tests check that the second message never reached the handler.
    """

    def __init__(self) -> None:
        self.session = ClaimSession()
        self.begin_count = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[ClaimSession]:
        self.begin_count += 1
        try:
            yield self.session
        except Exception:
            await self.session.rollback()
            raise
        else:
            await self.session.commit()


class RecordingSqsClient:
    """Stands in for the aioboto3 SQS client.

    `receive_message` returns each queued batch once and then nothing, so a consumer's
    `run` loop terminates instead of long-polling forever. `delete_message` is recorded
    rather than performed — "was this message deleted?" is the assertion behind every
    queue-hygiene test in this directory: an unparseable body stays on the queue for the
    redrive policy, and a failed handler stays on the queue for the visibility timeout.
    """

    def __init__(self, batches: list[list[dict[str, Any]]] | None = None) -> None:
        self.batches = list(batches or [])
        self.deleted: list[str] = []
        self.receive_calls = 0

    async def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.receive_calls += 1
        if not self.batches:
            return {}
        return {"Messages": self.batches.pop(0)}

    async def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted.append(ReceiptHandle)

    async def change_message_visibility(
        self, *, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int
    ) -> None:
        return None


@pytest.fixture
def sessions_factory() -> ConsumerSessionFactory:
    return ConsumerSessionFactory()


@pytest.fixture
def sqs() -> RecordingSqsClient:
    return RecordingSqsClient()
