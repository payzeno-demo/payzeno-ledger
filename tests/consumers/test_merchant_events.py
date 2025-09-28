"""`MerchantEventConsumer` — app/consumers/merchant_events.py.

Queue `payzeno-ledger-merchants`, four types, all of them projection writes. This consumer
is the only reason the ledger knows a merchant's settlement tolerance, reserve rate,
payout schedule or verified bank account exists. Without it every settlement raises
`ChargeProjectionNotFoundError`'s merchant-shaped sibling and the ledger is inert.

`merchant.created` is the one type that does more than project: it also bootstraps the
merchant's account set through `AccountResolver`, which is the single writer of `account`.
There are not three mechanisms for creating accounts; there is one writer with three
callers, and this is one of them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.consumers.merchant_events import MerchantEventConsumer

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 11, 45, tzinfo=UTC)


class Envelope:
    def __init__(self, event_type: str, payload: dict[str, Any], event_id: str = "evt_m1") -> None:
        self.id = event_id
        self.type = event_type
        self.version = 1
        self.occurred_at = NOW.isoformat()
        self.source = "payzeno-api"
        self.correlation_id = "req_m1"
        self.causation_id = None
        self.merchant_id = "mer_new"
        self.livemode = True
        self.payload = payload


class Settings:
    sqs_merchants_queue_url = "http://localstack/000000000000/payzeno-ledger-merchants"


class _NullProcessed:
    async def claim(self, session: Any, *, event_id: str, consumer: str) -> bool:
        return True


class _Null:
    pass


class _FixedClock:
    def now(self) -> datetime:
        return NOW


def _no_sqs():  # pragma: no cover
    raise AssertionError("no SQS client in this test")


def _consumer(sessions, monkeypatch: pytest.MonkeyPatch):
    routed: list[str] = []

    def _recorder(name: str):
        async def _handler(session: Any, payload: dict[str, Any], **kwargs: Any) -> None:
            routed.append(name)

        return _handler

    import app.consumers.merchant_events as module

    for handler in (
        "handle_merchant_created",
        "handle_merchant_updated",
        "handle_merchant_status_changed",
        "handle_bank_account_verified",
    ):
        monkeypatch.setattr(module, handler, _recorder(handler))

    consumer = MerchantEventConsumer(
        sessions,
        _NullProcessed(),
        _no_sqs,
        Settings(),
        merchants=_Null(),
        banks=_Null(),
        resolver=_Null(),
        clock=_FixedClock(),
    )
    return consumer, routed


async def test_queue_url_comes_from_settings(sessions_factory) -> None:
    consumer, _ = _consumer(sessions_factory, pytest.MonkeyPatch())

    assert consumer.queue_url == Settings.sqs_merchants_queue_url
    assert consumer.consumer_name == "merchant_events"


async def test_it_handles_exactly_four_types() -> None:
    assert MerchantEventConsumer.handled_types == frozenset(
        {
            "merchant.created",
            "merchant.updated",
            "merchant.status_changed",
            "merchant.bank_account_verified",
        }
    )


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("merchant.created", "handle_merchant_created"),
        ("merchant.updated", "handle_merchant_updated"),
        ("merchant.status_changed", "handle_merchant_status_changed"),
        ("merchant.bank_account_verified", "handle_bank_account_verified"),
    ],
)
async def test_each_type_reaches_its_own_handler(
    sessions_factory, monkeypatch: pytest.MonkeyPatch, event_type: str, expected: str
) -> None:
    consumer, routed = _consumer(sessions_factory, monkeypatch)

    await consumer.handle(
        sessions_factory.session,
        Envelope(event_type, {"merchant_id": "mer_new", "status": "active"}),
    )

    assert routed == [expected]


async def test_status_changed_does_not_carry_livemode(
    sessions_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A status change applies to both modes at once.

    `merchant.status_changed` is the one handler in this consumer that takes no
    `livemode`: restricting a merchant restricts their test traffic too, and passing a
    mode here would let half of it through.
    """
    seen: list[dict[str, Any]] = []

    async def _capture(session: Any, payload: dict[str, Any], **kwargs: Any) -> None:
        seen.append(kwargs)

    import app.consumers.merchant_events as module

    monkeypatch.setattr(module, "handle_merchant_status_changed", _capture)
    consumer = MerchantEventConsumer(
        sessions_factory,
        _NullProcessed(),
        _no_sqs,
        Settings(),
        merchants=_Null(),
        banks=_Null(),
        resolver=_Null(),
        clock=_FixedClock(),
    )

    await consumer.handle(
        sessions_factory.session,
        Envelope("merchant.status_changed", {"merchant_id": "mer_new", "status": "restricted"}),
    )

    assert "livemode" not in seen[0]
    assert seen[0]["occurred_at"].tzinfo is not None


async def test_occurred_at_is_parsed_into_an_aware_datetime(
    sessions_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every projection upsert is guarded on `source_occurred_at`.

    Comparing a naive datetime to an aware one raises, and it raises inside a consumer,
    which means the message is redelivered forever. Parsing here rather than in four
    handlers is the whole reason this method exists.
    """
    seen: list[datetime] = []

    async def _capture(session: Any, payload: dict[str, Any], **kwargs: Any) -> None:
        seen.append(kwargs["occurred_at"])

    import app.consumers.merchant_events as module

    monkeypatch.setattr(module, "handle_merchant_created", _capture)
    consumer = MerchantEventConsumer(
        sessions_factory,
        _NullProcessed(),
        _no_sqs,
        Settings(),
        merchants=_Null(),
        banks=_Null(),
        resolver=_Null(),
        clock=_FixedClock(),
    )

    envelope = Envelope("merchant.created", {"merchant_id": "mer_new"})
    envelope.occurred_at = "2026-04-16T11:45:00Z"

    await consumer.handle(sessions_factory.session, envelope)

    assert seen[0] == NOW
    assert seen[0].tzinfo is not None


async def test_an_unrouted_type_does_not_raise(
    sessions_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer, routed = _consumer(sessions_factory, monkeypatch)

    await consumer.handle(
        sessions_factory.session, Envelope("merchant.deleted", {"merchant_id": "mer_new"})
    )

    assert routed == []
