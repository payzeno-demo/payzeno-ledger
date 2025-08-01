"""Settlement reconciliation.

Two independent schedulers drive this package:

* ``ReconciliationSweepJob`` (900s) → :class:`ReconciliationService.reconcile_batch`
* ``RetryDrainJob`` (60s) → :class:`RetryScheduler.drain`

Both settle through the same :class:`SettlementPoster` instance, and both agree on
:data:`RETRYABLE_STATUSES` — the shared constant that keeps the two paths' notion of
"eligible" identical.
"""

from app.services.reconciliation.backlog import BacklogService
from app.services.reconciliation.constants import (
    IN_FLIGHT_STATUSES,
    MAX_ATTEMPTS,
    RETRYABLE_ERROR_CODES,
    RETRYABLE_STATUSES,
    TERMINAL_STATUSES,
)
from app.services.reconciliation.matcher import (
    ExactReferenceMatch,
    HeuristicAmountWindowMatch,
    ManualMatch,
    MatchStrategy,
    NetworkTransactionMatch,
    match_items,
)
from app.services.reconciliation.poster import SettlementPoster
from app.services.reconciliation.reconciler import ReconciliationService
from app.services.reconciliation.retry import RetryScheduler
from app.services.reconciliation.types import (
    BacklogBucket,
    MatchOutcome,
    ReconcilePassStats,
    SettlementResult,
)

__all__ = [
    "BacklogBucket",
    "BacklogService",
    "ExactReferenceMatch",
    "HeuristicAmountWindowMatch",
    "IN_FLIGHT_STATUSES",
    "MAX_ATTEMPTS",
    "ManualMatch",
    "MatchOutcome",
    "MatchStrategy",
    "NetworkTransactionMatch",
    "RETRYABLE_ERROR_CODES",
    "RETRYABLE_STATUSES",
    "ReconcilePassStats",
    "ReconciliationService",
    "RetryScheduler",
    "SettlementPoster",
    "SettlementResult",
    "TERMINAL_STATUSES",
    "match_items",
]
