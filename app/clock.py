"""Time as a dependency.

``SystemClock`` is the only place in ``app/`` that calls ``datetime.now``. Everything else
— backoff schedules, banking-calendar arithmetic, payout cutoffs, ``posted_at``,
``next_attempt_at``, every ``last_attempt_at`` — takes a :class:`~app.ports.Clock` and asks
it. That is what makes "the drain at 00:15Z on a Thursday before a bank holiday" a test
rather than a story.

The second implementation, ``FrozenClock``, lives in ``tests/doubles.py``. It is named in
``interfaces.md`` §3.4 and is deliberately *not* in this module: production code must not
be able to import a clock that does not move.

Layering: L-1.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.ports import Clock

__all__ = ["SystemClock", "utc_now"]


def utc_now() -> datetime:
    """Timezone-aware UTC, always.

    A naive datetime in this service is a bug: ``timestamptz`` columns silently coerce
    one using the *server's* timezone, and the settlement window arithmetic in
    ``BankingCalendar`` then lands a payout a day early roughly twice a year.
    """
    return datetime.now(tz=timezone.utc)


class SystemClock(Clock):
    """The production :class:`~app.ports.Clock`. One instance, built in ``app/container.py``.

    Stateless, so sharing it across every service, worker and consumer is free — and
    sharing matters: two clock instances is two answers to "now", and the reconciliation
    run's ``started_at``/``finished_at`` pair has to come from one.
    """

    __slots__ = ()

    def now(self) -> datetime:
        """The current instant, UTC-aware."""
        return utc_now()

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance only
        return "SystemClock()"
