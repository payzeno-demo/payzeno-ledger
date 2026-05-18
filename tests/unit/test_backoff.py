"""Retry backoff — app/domain/backoff.py.

Added by PAY-2059. Before it, `_mark_retryable` set nothing but the status and the drain
pulled the same item again 60 seconds later — which is how one degraded acquirer took 22
minutes of uninterrupted hammering during PAY-2041.

`next_attempt_at(now, attempt_count, base_seconds)` is `now + base * 2**attempt_count` with
±20% jitter. The jitter is the reason this is a function with an injectable RNG and not two
lines inlined into retry.py: without a seed there is nothing to assert.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.backoff import JITTER_FRACTION, next_attempt_at

NOW = datetime(2026, 4, 15, 23, 44, 0, tzinfo=UTC)


def test_jitter_fraction_is_twenty_percent() -> None:
    assert JITTER_FRACTION == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("attempt_count", "nominal_seconds"),
    [(0, 30), (1, 60), (2, 120), (3, 240), (4, 480), (5, 960)],
)
def test_delay_doubles_per_attempt(attempt_count: int, nominal_seconds: int) -> None:
    rng = random.Random(1)
    computed = next_attempt_at(NOW, attempt_count=attempt_count, base_seconds=30, rng=rng)

    delta = (computed - NOW).total_seconds()
    assert nominal_seconds * 0.8 <= delta <= nominal_seconds * 1.2


def test_result_is_timezone_aware_and_in_utc() -> None:
    computed = next_attempt_at(NOW, attempt_count=2, base_seconds=30, rng=random.Random(7))
    assert computed.tzinfo is not None
    assert computed.utcoffset() == timedelta(0)


def test_result_is_always_in_the_future() -> None:
    for attempt in range(0, 8):
        computed = next_attempt_at(NOW, attempt_count=attempt, base_seconds=30, rng=random.Random(attempt))
        assert computed > NOW


def test_jitter_actually_spreads_the_attempts() -> None:
    """4,113 items all marked retryable in the same 22-minute window.

    Without jitter they all become eligible at the same instant and the drain re-creates the
    thundering herd it was meant to break up.
    """
    values = {
        next_attempt_at(NOW, attempt_count=3, base_seconds=30, rng=random.Random(seed))
        for seed in range(200)
    }
    assert len(values) > 100


def test_jitter_is_bounded_both_ways() -> None:
    rng = random.Random(20260415)
    nominal = 30 * 2**4
    for _ in range(2_000):
        delta = (next_attempt_at(NOW, attempt_count=4, base_seconds=30, rng=rng) - NOW).total_seconds()
        assert nominal * 0.8 <= delta <= nominal * 1.2


def test_the_same_seed_reproduces_the_same_instant() -> None:
    a = next_attempt_at(NOW, attempt_count=2, base_seconds=30, rng=random.Random(42))
    b = next_attempt_at(NOW, attempt_count=2, base_seconds=30, rng=random.Random(42))
    assert a == b


def test_negative_attempt_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="attempt_count"):
        next_attempt_at(NOW, attempt_count=-1, base_seconds=30, rng=random.Random(1))


def test_delay_is_capped_so_a_stuck_item_still_gets_looked_at() -> None:
    # attempt_count is bounded by RECONCILE_MAX_ATTEMPTS long before this matters, but an
    # unbounded 2**n on a bad row parks it past the heat death of the settlement window.
    far = next_attempt_at(NOW, attempt_count=40, base_seconds=30, rng=random.Random(1))
    assert (far - NOW) <= timedelta(days=1)
