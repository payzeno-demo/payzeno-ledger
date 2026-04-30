"""The four test doubles the whole suite shares.

One rule: a double implements the **production protocol** from ``app/ports.py`` and
declares it as a base, so ``mypy --strict`` fails here rather than in the ninth test that
uses it, and so a signature change in ``ProcessorClient`` cannot quietly leave the suite
asserting against an interface nothing has any more.

``StaticFeatureFlags`` is re-exported rather than reimplemented: it is production code
(``app/flags.py``), wired into the ops CLI, and a second copy here would be a second
opinion about what an unknown flag does. Tests import it from ``tests.doubles`` because
that is where they look for doubles; the object is the real one.

``FrozenClock`` is the one double that is *only* a double. It is named in
``interfaces.md`` §3.4 as the second ``Clock`` implementation and it lives here, not in
``app/``, so production code cannot import a clock that does not move.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.flags import StaticFeatureFlags
from app.ports import CaptureResponse, CaptureStatus, Clock, EventPublisher, ProcessorClient

__all__ = [
    "CollectingPublisher",
    "FrozenClock",
    "RecordingProcessorClient",
    "StaticFeatureFlags",
]

#: The suite's default instant. The night of PAY-2041.
FIXED_NOW = dt.datetime(2026, 1, 22, 0, 15, tzinfo=dt.timezone.utc)


class FrozenClock(Clock):
    """A clock that does not move unless a test moves it.

    Everything time-shaped in this service takes a ``Clock``: backoff schedules, payout
    cutoffs, banking-calendar arithmetic, ``posted_at``, ``next_attempt_at``. Freezing it
    is what turns "the drain at 00:15Z on the Thursday before a bank holiday" from a story
    into an assertion.
    """

    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or FIXED_NOW

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, seconds: float) -> dt.datetime:
        """Move forward. Returns the new instant so a test can assert against it.

        Used by the backoff and cutoff tests, which need two readings of "now" that differ
        by a known amount — a real clock gives two readings that differ by an unknown one.
        """
        self._now = self._now + dt.timedelta(seconds=seconds)
        return self._now

    def set(self, moment: dt.datetime) -> None:
        """Jump to an absolute instant, for calendar-boundary cases."""
        self._now = moment


class CollectingPublisher(EventPublisher):
    """An :class:`~app.ports.EventPublisher` that keeps everything instead of sending it.

    Accepts ``session=`` and ignores it. The production ``OutboxPublisher`` *requires* a
    session because it stages into the caller's transaction; this one takes it so the two
    are substitutable, and drops it because there is no transaction to roll back.

    Consequence worth knowing: a test that asserts "nothing was published" after a failed
    settlement is asserting something weaker here than in production, where the rollback
    does the work. That is what ``tests/integration/test_settlement_lifecycle.py`` is for.
    """

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        merchant_id: str | None,
        correlation_id: str,
        causation_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        envelope_id = f"evt_{len(self.published) + 1:04d}"
        self.published.append(
            {
                "id": envelope_id,
                "type": event_type,
                "payload": payload,
                "merchant_id": merchant_id,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "livemode": kwargs.get("livemode", True),
            }
        )
        return envelope_id

    def event_types(self) -> list[str]:
        """Every type published, **in order**.

        Order matters in more than one assertion: ``settlement.item_settled`` before
        ``settlement.duplicate_detected`` is the difference between "the first caller won
        and the second found out" and "something published a duplicate for no reason".
        """
        return [event["type"] for event in self.published]

    def payload_for(self, event_type: str) -> dict[str, Any]:
        """The payload of the **last** event of this type.

        Last rather than first: the interesting event in a two-caller test is the one the
        loser published.
        """
        for event in reversed(self.published):
            if event["type"] == event_type:
                return dict(event["payload"])
        raise AssertionError(
            f"no {event_type!r} was published; got {self.event_types()}"
        )

    def count(self, event_type: str) -> int:
        """How many of this type went out. Two ``item_settled`` for one item is the bug."""
        return sum(1 for event in self.published if event["type"] == event_type)


class RecordingProcessorClient(ProcessorClient):
    """A :class:`~app.ports.ProcessorClient` that records calls and can be made to fail.

    ``call_order`` is the reason this exists rather than a bare mock. Several assertions
    are about *sequence* — ``confirm_settlement`` runs unconditionally and first, and
    ``capture_deferred`` runs only after the idempotency claim succeeded — and a mock that
    records per-method call lists cannot express "before".

    The integration suite subclasses the same idea with an ``asyncio.Barrier`` inside
    ``capture_deferred`` so the two callers interleave deterministically. That barrier is
    test-only scaffolding; this class is the plain version.
    """

    def __init__(
        self,
        *,
        confirm_raises: Exception | None = None,
        capture_raises: Exception | None = None,
        capture_state: str = "captured",
        settlement_file: bytes = b"",
    ) -> None:
        self.confirm_calls: list[dict[str, Any]] = []
        self.capture_calls: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, Any]] = []
        self.fetches: list[tuple[str, dt.date]] = []
        self.call_order: list[str] = []
        self._confirm_raises = confirm_raises
        self._capture_raises = capture_raises
        self._capture_state = capture_state
        self._settlement_file = settlement_file

    async def confirm_settlement(
        self, acquirer: str, acquirer_reference: str, batch_id: str
    ) -> None:
        self.call_order.append("confirm_settlement")
        self.confirm_calls.append(
            {
                "acquirer": acquirer,
                "acquirer_reference": acquirer_reference,
                "batch_id": batch_id,
            }
        )
        if self._confirm_raises is not None:
            raise self._confirm_raises

    async def capture_deferred(
        self,
        charge_id: str,
        amount_minor: int,
        currency: str,
        reference: str,
        *,
        idempotency_key: str,
    ) -> CaptureResponse:
        self.call_order.append("capture_deferred")
        self.capture_calls.append(
            {
                "charge_id": charge_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "reference": reference,
                "idempotency_key": idempotency_key,
            }
        )
        if self._capture_raises is not None:
            raise self._capture_raises
        return CaptureResponse(
            captured=True, reference=idempotency_key, captured_at=FIXED_NOW
        )

    async def get_capture_status(self, acquirer: str, idempotency_key: str) -> CaptureStatus:
        self.call_order.append("get_capture_status")
        self.status_calls.append(
            {"acquirer": acquirer, "idempotency_key": idempotency_key}
        )
        captured = any(
            call["idempotency_key"] == idempotency_key for call in self.capture_calls
        )
        if captured:
            return CaptureStatus(state="captured", reference=idempotency_key)
        return CaptureStatus(state=self._capture_state, reference=None)  # type: ignore[arg-type]

    async def fetch_settlement_file(self, acquirer: str, processing_date: dt.date) -> bytes:
        self.call_order.append("fetch_settlement_file")
        self.fetches.append((acquirer, processing_date))
        return self._settlement_file
