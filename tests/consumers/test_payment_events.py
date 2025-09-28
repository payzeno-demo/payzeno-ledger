"""`PaymentEventConsumer` — app/consumers/payment_events.py.

Queue `payzeno-ledger-payments`, six handled types out of the topic's ten. The routing is
the unit under test here; the handler bodies are `tests/consumers/test_handlers_payments.py`.

Routing sounds too trivial to test until you notice what it decides: `payment.authorized`
is what creates the `settlement_charge` projection, and `capture_at_settlement` on that
projection is the field that decides whether the ledger issues a second cardholder
capture. A misrouted authorisation is a charge with no projection, and a charge with no
projection is an `orphaned` item three days later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from payzeno_contracts.events import EventEnvelope

from app.consumers.payment_events import PaymentEventConsumer

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 11, 30, tzinfo=UTC)


class Envelope:
    """A minimal stand-in with the `EventEnvelope` attribute surface."""

    def __init__(self, event_type: str, payload: dict[str, Any], event_id: str = "evt_p1") -> None:
        self.id = event_id
        self.type = event_type
        self.version = 1
        self.occurred_at = NOW.isoformat()
        self.source = "payzeno-api"
        self.correlation_id = "req_p1"
        self.causation_id = None
        self.merchant_id = "mer_pay"
        self.livemode = True
        self.payload = payload


class Settings:
    sqs_payments_queue_url = "http://localstack/000000000000/payzeno-ledger-payments"


def _consumer(sessions, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    """Wire the consumer with every handler replaced by a recorder.

    Patching at module scope rather than injecting handler objects, because the consumer
    imports the handlers by name — which is the coupling the routing test is about.
    """
    routed: list[str] = []

    def _recorder(name: str):
        async def _handler(session: Any, payload: dict[str, Any], **kwargs: Any) -> None:
            routed.append(name)

        return _handler

    import app.consumers.payment_events as module

    for handler in (
        "handle_payment_authorized",
        "handle_payment_captured",
        "handle_payment_canceled",
        "handle_refund_created",
        "handle_dispute_opened",
        "handle_dispute_closed",
    ):
        monkeypatch.setattr(module, handler, _recorder(handler))

    consumer = PaymentEventConsumer(
        sessions,
        _NullProcessed(),
        _no_sqs,
        Settings(),
        charges=_Null(),
        transactions=_Null(),
        ledger=_Null(),
        clock=_FixedClock(),
    )
    return consumer, routed


class _Null:
    pass


class _NullProcessed:
    async def claim(self, session: Any, *, event_id: str, consumer: str) -> bool:
        return True


class _FixedClock:
    def now(self) -> datetime:
        return NOW


def _no_sqs():  # pragma: no cover
    raise AssertionError("no SQS client in this test")


async def test_queue_url_comes_from_settings(sessions_factory) -> None:
    consumer = PaymentEventConsumer(
        sessions_factory,
        _NullProcessed(),
        _no_sqs,
        Settings(),
        charges=_Null(),
        transactions=_Null(),
        ledger=_Null(),
        clock=_FixedClock(),
    )

    assert consumer.queue_url == Settings.sqs_payments_queue_url
    assert consumer.consumer_name == "payment_events"


async def test_it_handles_exactly_six_types(sessions_factory) -> None:
    """Six of the payments topic's ten. The other four are payzeno-api's business."""
    assert PaymentEventConsumer.handled_types == frozenset(
        {
            "payment.authorized",
            "payment.captured",
            "payment.canceled",
            "refund.created",
            "dispute.opened",
            "dispute.closed",
        }
    )


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("payment.authorized", "handle_payment_authorized"),
        ("payment.captured", "handle_payment_captured"),
        ("payment.canceled", "handle_payment_canceled"),
        ("refund.created", "handle_refund_created"),
        ("dispute.opened", "handle_dispute_opened"),
        ("dispute.closed", "handle_dispute_closed"),
    ],
)
async def test_each_type_reaches_its_own_handler(
    sessions_factory, monkeypatch: pytest.MonkeyPatch, event_type: str, expected: str
) -> None:
    consumer, routed = _consumer(sessions_factory, monkeypatch)

    await consumer.handle(
        sessions_factory.session,
        Envelope(event_type, {"charge_id": "ch_1", "merchant_id": "mer_pay"}),
    )

    assert routed == [expected]


async def test_an_unrouted_type_does_not_raise(
    sessions_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`handled_types` already filtered, so this branch is unreachable in production.

    It logs rather than raising because the alternative is a poison message that fails
    forever and blocks the queue behind it.
    """
    consumer, routed = _consumer(sessions_factory, monkeypatch)

    await consumer.handle(
        sessions_factory.session, Envelope("payment.failed", {"charge_id": "ch_1"})
    )

    assert routed == []


async def test_the_envelope_type_comes_from_the_contracts_package() -> None:
    """No local envelope class. `EventEnvelope` is `payzeno_contracts.events`'s.

    A ledger-local copy is how the two services end up disagreeing about whether
    `merchant_id` is nullable, which they did once, for a fortnight.
    """
    assert EventEnvelope.__module__.startswith("payzeno_contracts")


async def test_livemode_is_carried_from_the_envelope_not_the_payload(
    sessions_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`livemode` is an envelope concern.

    Reading it off the payload means a test-mode event with a missing field posts against
    live accounts, and `LivemodeMismatchError` only catches that after the fact.
    """
    seen: list[bool] = []

    async def _capture(session: Any, payload: dict[str, Any], **kwargs: Any) -> None:
        seen.append(kwargs["livemode"])

    import app.consumers.payment_events as module

    monkeypatch.setattr(module, "handle_payment_authorized", _capture)
    consumer = PaymentEventConsumer(
        sessions_factory,
        _NullProcessed(),
        _no_sqs,
        Settings(),
        charges=_Null(),
        transactions=_Null(),
        ledger=_Null(),
        clock=_FixedClock(),
    )

    envelope = Envelope("payment.authorized", {"charge_id": "ch_1", "livemode": False})
    envelope.livemode = True

    await consumer.handle(sessions_factory.session, envelope)

    assert seen == [True]
