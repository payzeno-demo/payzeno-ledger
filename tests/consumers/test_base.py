"""`BaseConsumer` — app/consumers/base.py.

The dedupe claim is the point of this file. It is INSERT-first:

    INSERT INTO processed_event (event_id, consumer) VALUES (...)
    ON CONFLICT DO NOTHING RETURNING event_id

in the **same transaction** as the handler's side effects. There is deliberately no
`_already_processed` / `_mark_processed` pair, because check-then-act around a side effect
is precisely the shape of PAY-2041 and ADR 0011 forbids it in the money path — and this
is the money path.

The other properties asserted here are the queue-hygiene ones, which are boring right up
until an outage: an unparseable message is left on the queue for the redrive policy rather
than deleted, and a message whose handler failed is not deleted either.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from payzeno_contracts.events import EventEnvelope

from app.consumers.base import (
    LONG_POLL_SECONDS,
    MAX_MESSAGES_PER_RECEIVE,
    BaseConsumer,
    supported_types,
)
from app.errors import PayzenoLedgerError
from app.repositories.processed_event import ProcessedEventRepository

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 11, 0, tzinfo=UTC)


def envelope_body(
    *,
    event_id: str = "evt_01HZ",
    event_type: str = "payment.authorized",
    payload: dict[str, Any] | None = None,
    wrap_in_sns: bool = True,
) -> dict[str, Any]:
    inner = {
        "id": event_id,
        "type": event_type,
        "version": 1,
        "occurred_at": NOW.isoformat(),
        "source": "payzeno-api",
        "correlation_id": "req_1",
        "causation_id": None,
        "merchant_id": "mer_1",
        "livemode": True,
        "payload": payload or {"charge_id": "ch_1"},
    }
    body = json.dumps({"Message": json.dumps(inner)}) if wrap_in_sns else json.dumps(inner)
    return {"Body": body, "ReceiptHandle": f"rh_{event_id}"}


class ClaimingProcessedEvents(ProcessedEventRepository):
    """`claim` against a set, with the same INSERT ... ON CONFLICT semantics."""

    def __init__(self) -> None:
        super().__init__()
        self.claimed: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, str]] = []

    async def claim(self, session: Any, *, event_id: str, consumer: str) -> bool:
        self.calls.append((event_id, consumer))
        key = (event_id, consumer)
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class SpyConsumer(BaseConsumer):
    consumer_name: ClassVar[str] = "spy"
    handled_types: ClassVar[frozenset[str]] = frozenset(
        {"payment.authorized", "payment.captured"}
    )

    def __init__(self, sessions, processed, *, raises: Exception | None = None) -> None:
        super().__init__(sessions, processed, _no_sqs)
        self.queue_url = "http://localstack/000000000000/payzeno-ledger-spy"
        self.handled: list[str] = []
        self.raises = raises

    async def handle(self, session: Any, event: EventEnvelope) -> None:
        if self.raises is not None:
            raise self.raises
        self.handled.append(event.id)


def _no_sqs():  # pragma: no cover - the long-poll loop is not exercised here
    raise AssertionError("this consumer test never opens an SQS client")


async def test_base_consumer_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseConsumer(None, None, None)  # type: ignore[abstract]


async def test_a_first_delivery_is_claimed_and_handled(sessions_factory) -> None:
    processed = ClaimingProcessedEvents()
    consumer = SpyConsumer(sessions_factory, processed)

    deletable = await consumer._handle_message(envelope_body())  # noqa: SLF001

    assert deletable is True
    assert consumer.handled == ["evt_01HZ"]
    assert processed.calls == [("evt_01HZ", "spy")]


async def test_a_redelivery_is_claimed_once_and_the_handler_is_skipped(
    sessions_factory,
) -> None:
    """SQS is at-least-once. A redelivery must not post the ledger a second time."""
    processed = ClaimingProcessedEvents()
    consumer = SpyConsumer(sessions_factory, processed)

    first = await consumer._handle_message(envelope_body())  # noqa: SLF001
    second = await consumer._handle_message(envelope_body())  # noqa: SLF001

    assert (first, second) == (True, True)
    assert consumer.handled == ["evt_01HZ"], "the handler ran twice for one event"


async def test_the_claim_is_scoped_to_the_consumer(sessions_factory) -> None:
    """Two consumers on two queues legitimately see the same event id.

    The primary key is `(event_id, consumer)`. A claim keyed on the event alone means
    whichever consumer wins the race silently starves the other.
    """
    processed = ClaimingProcessedEvents()
    payments = SpyConsumer(sessions_factory, processed)
    merchants = SpyConsumer(sessions_factory, processed)
    merchants.consumer_name = "spy_two"

    await payments._handle_message(envelope_body())  # noqa: SLF001
    await merchants._handle_message(envelope_body())  # noqa: SLF001

    assert payments.handled == ["evt_01HZ"]
    assert merchants.handled == ["evt_01HZ"]


async def test_an_unhandled_type_is_deleted_without_being_claimed(
    sessions_factory,
) -> None:
    """Subscribed by topic, filtered by type.

    It is on our queue, so it will never be anyone else's. Leaving it there would grow
    the queue forever behind a redrive policy that never triggers.
    """
    processed = ClaimingProcessedEvents()
    consumer = SpyConsumer(sessions_factory, processed)

    deletable = await consumer._handle_message(  # noqa: SLF001
        envelope_body(event_type="dispute.evidence_submitted")
    )

    assert deletable is True
    assert processed.calls == []
    assert consumer.handled == []


async def test_an_unparseable_message_is_left_on_the_queue(sessions_factory) -> None:
    """Deleting it destroys the only copy of a message nobody could read."""
    processed = ClaimingProcessedEvents()
    consumer = SpyConsumer(sessions_factory, processed)

    deletable = await consumer._handle_message(  # noqa: SLF001
        {"Body": "not json at all", "ReceiptHandle": "rh_bad"}
    )

    assert deletable is False
    assert processed.calls == []


async def test_a_failed_handler_leaves_the_message_for_redelivery(
    sessions_factory,
) -> None:
    """And the claim rolls back with it, so the retry is not deduped away."""
    processed = ClaimingProcessedEvents()
    consumer = SpyConsumer(
        sessions_factory, processed, raises=PayzenoLedgerError("account frozen")
    )

    deletable = await consumer._handle_message(envelope_body())  # noqa: SLF001

    assert deletable is False
    assert sessions_factory.session.rolled_back is True


async def test_a_raw_sns_body_and_a_bare_envelope_both_parse(sessions_factory) -> None:
    """SNS-to-SQS nests the payload under `Message`; a direct SQS publish does not."""
    processed = ClaimingProcessedEvents()
    consumer = SpyConsumer(sessions_factory, processed)

    await consumer._handle_message(envelope_body(event_id="evt_a"))  # noqa: SLF001
    await consumer._handle_message(  # noqa: SLF001
        envelope_body(event_id="evt_b", wrap_in_sns=False)
    )

    assert consumer.handled == ["evt_a", "evt_b"]


async def test_supported_types_rejects_a_typo() -> None:
    """A typo in `handled_types` is a queue that silently drops everything.

    Making it an ImportError instead is the cheapest possible version of that lesson.
    """
    assert supported_types("payment.authorized") == frozenset({"payment.authorized"})

    with pytest.raises(ValueError):
        supported_types("payment.authorised")


async def test_long_poll_settings_are_the_cheap_ones() -> None:
    assert LONG_POLL_SECONDS == 20
    assert MAX_MESSAGES_PER_RECEIVE == 10


async def test_run_stops_when_the_event_is_set(sessions_factory) -> None:
    """The loop checks `stop` before every receive, so shutdown is bounded by one poll."""
    processed = ClaimingProcessedEvents()
    consumer = SpyConsumer(sessions_factory, processed)
    stop = asyncio.Event()
    stop.set()

    opened: list[str] = []

    class _Client:
        async def __aenter__(self) -> Any:
            opened.append("in")
            return self

        async def __aexit__(self, *exc: Any) -> None:
            opened.append("out")

        async def receive_message(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("receive_message called after stop was set")

    consumer._sqs_client_factory = lambda: _Client()  # noqa: SLF001

    await consumer.run(stop)

    assert opened == ["in", "out"]
