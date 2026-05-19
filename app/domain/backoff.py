"""Exponential backoff with jitter — PAY-2059.

`domain-model.md` §8: ``_mark_retryable`` sets

    next_attempt_at = now() + RECONCILE_RETRY_BACKOFF_BASE_SECONDS * 2**attempt_count

with ±20% jitter. ``pix_reconciliation_item_retryable`` is
``(batch_id, next_attempt_at) where status in ('pending','retryable')`` and the drain
filters ``next_attempt_at <= now()``.

Before this module existed the drain ordered by ``last_attempt_at`` and filtered on
nothing, which is how four drains pulling 200 items every 60s hammered an acquirer that
was already returning 504s at up to 800 capture attempts a minute — during exactly the
degradation that caused PAY-2041.

Pure: no clock of its own, no ``random`` seeding at import. ``now`` comes from the
caller's :class:`~app.ports.Clock` and the jitter source is injectable so
``tests/unit/test_backoff.py`` can assert exact boundaries.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final

from app.errors import ValidationError

#: Default base delay. `Settings.reconcile_retry_backoff_base_seconds` overrides it.
DEFAULT_BASE_SECONDS: Final[int] = 30

#: ±20%, the same jitter band webhook delivery uses in payzeno-api (`domain-model.md` §10).
JITTER_FRACTION: Final[float] = 0.20

#: Past this the doubling stops. 2**12 * 30s is a bit over 34 hours; an item that has
#: failed twelve times is a manual investigation, not a scheduling problem.
MAX_EXPONENT: Final[int] = 12

#: Hard ceiling regardless of exponent, so a misconfigured base cannot park an item for
#: a week. Six hours is one `FundingMatchJob` cycle short of a working day.
MAX_DELAY_SECONDS: Final[int] = 6 * 60 * 60

JitterSource = Callable[[], float]


def _default_jitter() -> float:
    """Uniform in ``[-1.0, 1.0]``. Not seeded — CI would then share one schedule."""
    return random.uniform(-1.0, 1.0)  # noqa: S311  (scheduling jitter, not a secret)


def delay_seconds(
    attempt_count: int,
    *,
    base_seconds: int = DEFAULT_BASE_SECONDS,
    jitter: JitterSource | None = None,
) -> int:
    """Backoff delay in seconds for the given attempt.

    ``attempt_count`` is the number of attempts already made, so the first retry (after
    one failure) waits ``base_seconds * 2``.
    """
    if attempt_count < 0:
        raise ValidationError(
            "attempt_count must not be negative", details={"attempt_count": attempt_count}
        )
    if base_seconds <= 0:
        raise ValidationError(
            "backoff base must be positive", details={"base_seconds": base_seconds}
        )

    exponent = min(attempt_count, MAX_EXPONENT)
    raw = min(base_seconds * (2**exponent), MAX_DELAY_SECONDS)

    source = jitter or _default_jitter
    offset = raw * JITTER_FRACTION * _clamp(source())
    return max(1, int(raw + offset))


def next_attempt_at(
    now: datetime,
    attempt_count: int,
    *,
    base_seconds: int = DEFAULT_BASE_SECONDS,
    jitter: JitterSource | None = None,
) -> datetime:
    """The value written to ``reconciliation_item.next_attempt_at``.

    Called by ``RetryScheduler._mark_retryable`` and by
    ``ReconciliationService._mark_retryable``; both pass
    ``Settings.reconcile_retry_backoff_base_seconds``.
    """
    return now + timedelta(
        seconds=delay_seconds(attempt_count, base_seconds=base_seconds, jitter=jitter)
    )


def bounds_seconds(attempt_count: int, *, base_seconds: int = DEFAULT_BASE_SECONDS) -> tuple[int, int]:
    """The ``(min, max)`` delay the jitter band allows for `attempt_count`.

    ``docs/runbooks/reconciliation.md`` quotes these when an operator asks why an item is
    not being picked up yet, and ``tests/unit/test_backoff.py`` asserts every sampled
    delay falls inside them.
    """
    exponent = min(max(attempt_count, 0), MAX_EXPONENT)
    raw = min(base_seconds * (2**exponent), MAX_DELAY_SECONDS)
    low = max(1, int(raw * (1 - JITTER_FRACTION)))
    high = max(low, int(raw * (1 + JITTER_FRACTION)))
    return low, high


def is_due(next_attempt: datetime | None, now: datetime) -> bool:
    """Whether an item is eligible for the drain.

    A null ``next_attempt_at`` means "never scheduled", which for rows written before
    migration ``0024`` means immediately due. The column is NOT NULL with a
    ``default now()`` from that migration onward, so this branch only fires against
    fixtures and the ops CLI's ad-hoc queries.
    """
    return next_attempt is None or next_attempt <= now


def _clamp(value: float) -> float:
    """Keep an injected jitter source inside ``[-1.0, 1.0]``."""
    if value < -1.0:
        return -1.0
    if value > 1.0:
        return 1.0
    return value
