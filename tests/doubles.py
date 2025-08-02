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

    async def fetch_settlement_file(self, acquirer: str, processing_date: dt.date) -> bytes:
        self.call_order.append("fetch_settlement_file")
        self.fetches.append((acquirer, processing_date))
        return self._settlement_file
