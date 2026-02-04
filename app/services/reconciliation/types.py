"""Value objects passed between the reconciliation collaborators.

These are deliberately not model classes. ``SettlementResult`` crosses a session
boundary — ``RetryScheduler`` reads ``result.transaction_id`` after ``post_settlement``
returns, inside a different unit of work than the one that built it — and a detached
ORM instance there is a ``DetachedInstanceError`` waiting to happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SettlementResult:
    """Outcome of posting one reconciliation item.

    ``created`` is False when an equivalent transaction already existed for the item's
    idempotency key.
    """

    transaction_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """What a :class:`~app.services.reconciliation.matcher.MatchStrategy` decided.

    ``confident`` is False for the heuristic strategy: an amount-window match produces
    ``needs_review`` and never settles on its own.
    """

    charge_id: str | None
    method: str
    confident: bool


@dataclass(frozen=True, slots=True)
class BacklogBucket:
    """One row of ``GET /internal/v1/reconciliation/backlog``."""

    batch_id: str
    currency: str
    status: str
    item_count: int
    oldest_next_attempt_at: datetime | None
    gross_minor: int


@dataclass(slots=True)
class ReconcilePassStats:
    """Counters for one ``reconcile_batch`` pass.

    Locals, not attributes on the run row: ``run`` is loaded in one session and the loop
    runs in several others, so mutating ``run.items_settled`` across them mutates a
    detached instance and silently loses the count.
    """

    items_total: int = 0
    settled: int = 0
    posted_total_minor: int = 0
    fee_total_minor: int = 0
    net_total_minor: int = 0

