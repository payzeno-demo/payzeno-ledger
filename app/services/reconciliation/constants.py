"""Shared status sets for the reconciliation subsystem.

Both the batch sweep and the retry drain import RETRYABLE_STATUSES so the two paths
never disagree about what is eligible for settlement.
"""

from typing import Final

RETRYABLE_STATUSES: Final[frozenset[str]] = frozenset({"pending", "retryable"})
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"settled", "failed", "orphaned"})
MAX_ATTEMPTS: Final[int] = 6          # default for Settings.reconcile_max_attempts

# A timeout on a capture is the one state where we do NOT know whether the cardholder was
# charged. These never go down the retry path; they go to ProcessorClient.get_capture_status
# and the outcome decides. PAY-2060.
#
# `processor_timeout` sat in RETRYABLE_ERROR_CODES above for the first nine months and was
# moved here by PAY-2060. That was a second double-charge mechanism, independent of
# PAY-2041 and untouched by either fix for it: it needed no concurrency at all, only an
# acquirer slow enough to time out, and it fired on every degradation.
INDETERMINATE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"processor_timeout", "processor_connection_reset"}
)

#: Statuses a batch must be in before a reconciliation run may start against it.
RECONCILABLE_BATCH_STATUSES: Final[frozenset[str]] = frozenset(
    {"closed", "partially_reconciled"}
)

#: Line types that carry a settlement_charge and therefore a merchant balance movement.
CHARGE_BEARING_LINE_TYPES: Final[frozenset[str]] = frozenset(
    {"sale", "refund", "chargeback", "chargeback_reversal"}
)

