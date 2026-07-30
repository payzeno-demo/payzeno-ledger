"""The one exception tree — thirty classes, one base, no ad-hoc raises.

Every failure this service can express is a class in here, and every class carries two
class-level facts: a stable ``code`` the console branches on and an ``http_status``
payzeno-api's ``LedgerHttpClient`` branches on. Neither is ever passed in at the raise
site. That is deliberate and it is the reason this file is thirty classes rather than one
class with a ``status=`` argument: the status is a property of the *condition*, not of the
caller's mood, and letting a raise site choose it is how ``account_frozen`` spent two
months returning 500, tripping payzeno-api's circuit breaker and paging a human for what
was a policy decision (see ``docs/adr/0009-double-entry-invariants.md``).

``app/api/error_handlers.py`` registers exactly one handler for
:class:`PayzenoLedgerError` and is the only thing in the repo that builds an error
response. Nothing else constructs a ``JSONResponse`` with an error body.

Layering: this module sits at L-1. It imports nothing from ``app/`` at all — not even
``app.logging`` — because every other layer imports it (71 modules at last count) and a
cycle here is a cycle everywhere.
"""

from __future__ import annotations

from typing import Any, ClassVar

__all__ = [
    "AccountFrozenError",
    "AccountNotFoundError",
    "BankAccountProjectionNotFoundError",
    "BankAccountUnusableError",
    "BatchNotFoundError",
    "BatchNotReconcilableError",
    "BusPublishError",
    "ChargeProjectionNotFoundError",
    "CurrencyMismatchError",
    "DualControlRequiredError",
    "DuplicateDisputeError",
    "DuplicateSettlementError",
    "IdempotencyConflictError",
    "InsufficientBalanceError",
    "LedgerIntegrityError",
    "LivemodeMismatchError",
    "NegativeAmountError",
    "NotFoundError",
    "OrphanedItemError",
    "PayoutBlockedError",
    "PayoutError",
    "PayzenoLedgerError",
    "ProcessorIndeterminateError",
    "ProcessorUnavailableError",
    "ReconciliationItemNotFoundError",
    "RetryExhaustedError",
    "RetryableSettlementError",
    "SettlementError",
    "SettlementLockedError",
    "SettlementVarianceExceededError",
    "TransactionNotFoundError",
    "UnbalancedTransactionError",
    "UpstreamError",
    "ValidationError",
]


class PayzenoLedgerError(Exception):
    """Root of the tree. Everything raised on purpose in this service is one of these.

    ``details`` is free-form and lands verbatim under ``error.details`` in the ``ApiError``
    envelope, so it must only ever contain values that are safe to hand to another
    service: ids, counts, statuses, currency codes. Never a PAN, never a bank account
    number, never a raw acquirer payload. ``app/middleware/redaction.py`` is a backstop,
    not a licence.
    """

    #: Stable machine-readable code. The console switches on it; do not reword.
    code: ClassVar[str] = "internal_error"

    #: HTTP status ``app/api/error_handlers.py`` renders this condition as.
    http_status: ClassVar[int] = 500

    def __init__(self, message: str | None = None, /, **details: Any) -> None:
        # `code` is normally a class fact. Two conditions genuinely carry a *runtime*
        # code — the acquirer's, forwarded verbatim — and those pass it as a keyword.
        # See RetryableSettlementError and the UpstreamError branch in app/clients/http.py.
        override = details.pop("code", None)
        if override is not None:
            self.code = str(override)
        self.message = message or self.code
        self.details: dict[str, Any] = details
        super().__init__(self.message)

    def __str__(self) -> str:
        if self.code in self.message:
            return self.message
        return f"{self.code}: {self.message}"

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance only
        return f"{type(self).__name__}(code={self.code!r}, details={self.details!r})"


# ---------------------------------------------------------------------------------------
# Request-shaped failures
# ---------------------------------------------------------------------------------------


class ValidationError(PayzenoLedgerError):
    """The request is well-formed JSON and still wrong.

    FastAPI's own ``RequestValidationError`` covers schema violations; this covers the
    rules a schema cannot express — a window wider than 31 days, a ``to`` before a
    ``from``, an adjustment with no lines.
    """

    code: ClassVar[str] = "validation_failed"
    http_status: ClassVar[int] = 422


class NotFoundError(PayzenoLedgerError):
    """Nothing with that id exists, or nothing the caller may see.

    The ledger does not distinguish the two. It is an internal service behind
    payzeno-api's authorisation, so there is no enumeration risk to trade against a
    legible error, but there is also no reason to leak which merchants exist.
    """

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


class AccountNotFoundError(NotFoundError):
    """No ``account`` row. Raised by ``AccountRepository.get_or_raise``.

    Almost always a caller bug rather than a data problem: ``AccountResolver`` creates
    the chart-of-accounts rows lazily, so a missing account means someone asked for a
    ``(merchant, type, currency, livemode)`` tuple that is not a real account shape.
    """


class TransactionNotFoundError(NotFoundError):
    """No ``ledger_transaction`` row. Raised by ``LedgerTransactionRepository``."""


class BatchNotFoundError(NotFoundError):
    """No ``settlement_batch`` row. Raised by ``SettlementBatchRepository``."""


class ReconciliationItemNotFoundError(NotFoundError):
    """No ``reconciliation_item`` row.

    Reachable from the retry route with a stale id — the console keeps item ids in a
    query cache and an operator can click retry on a batch that was archived under them.
    """


class ChargeProjectionNotFoundError(NotFoundError):
    """No ``settlement_charge`` projection for a matched ``charge_id``.

    This means the projection is *behind*, not that the charge does not exist:
    ``payment.authorized`` has not been consumed yet. It is distinct from
    :class:`OrphanedItemError`, which is an item that was never matched at all — the
    poster checks ``item.charge_id is None`` first precisely so the two never blur.
    """


class BankAccountProjectionNotFoundError(NotFoundError):
    """No ``bank_account_projection`` for the merchant.

    Raised by ``BankAccountProjectionRepository.get_default``. A payout cannot be
    initiated without one and the rails all fail closed.
    """


# ---------------------------------------------------------------------------------------
# Ledger integrity — the only genuinely 500-class failures in the tree
# ---------------------------------------------------------------------------------------


class LedgerIntegrityError(PayzenoLedgerError):
    """A double-entry invariant does not hold.

    This one really is a server fault: either the posting rules produced something
    unbalanced or the stored state has drifted. ``LedgerAuditService`` raises it from the
    nightly trial balance and publishes ``ledger.imbalance_detected`` alongside.
    """

    code: ClassVar[str] = "ledger_imbalance"
    http_status: ClassVar[int] = 500


class UnbalancedTransactionError(LedgerIntegrityError):
    """Debits != credits within a currency. Invariant 1, ``LedgerPoster.post``."""


class NegativeAmountError(LedgerIntegrityError):
    """A posting line carries a non-positive ``amount_minor``.

    Direction lives in ``direction``, never in the sign. A negative amount means someone
    encoded a credit as a negative debit, and two such lines net to the right number
    while every per-account aggregate is wrong.
    """


class LivemodeMismatchError(LedgerIntegrityError):
    """Test-mode and live-mode rows met inside one transaction.

    The single worst thing this service can do quietly, which is why it is an integrity
    error and not a validation error: a sandbox charge that lands on a live merchant's
    balance is a payout of real money.
    """


class CurrencyMismatchError(LedgerIntegrityError):
    """Two currencies inside one transaction, or an entry against a foreign-currency account.

    400 rather than 500 — unlike its siblings this is reachable by a caller posting a
    badly built request, and FX never shipped, so there is no legitimate cross-currency
    transaction to be tolerant of.
    """

    code: ClassVar[str] = "currency_mismatch"
    http_status: ClassVar[int] = 400


# ---------------------------------------------------------------------------------------
# Business states that look like faults and are not
# ---------------------------------------------------------------------------------------


class AccountFrozenError(PayzenoLedgerError):
    """The target account is ``frozen`` or ``closed``.

    **409, not 500.** Set deliberately through ``POST /internal/v1/accounts/{id}/freeze``
    by risk. Returning 500 for a policy decision burns the error budget, trips
    payzeno-api's breaker for every other merchant, and pages someone who cannot fix it.
    """

    code: ClassVar[str] = "account_frozen"
    http_status: ClassVar[int] = 409


class IdempotencyConflictError(PayzenoLedgerError):
    """The idempotency key exists with a different request fingerprint.

    The caller reused a key for a materially different body. Distinct from a *replay*,
    which returns the original result and is not an error at all.
    """

    code: ClassVar[str] = "duplicate_settlement"
    http_status: ClassVar[int] = 409


class DuplicateSettlementError(IdempotencyConflictError):
    """The same settlement was posted twice.

    Carries ``existing_transaction_id`` so the 409 body tells the caller which row won —
    ``api-surface.md`` §10.2. Since PR #172 the money path asks
    ``LedgerPoster.post(..., on_conflict='return_existing')`` instead of raising, so this
    is now reached only by ``POST /internal/v1/transactions`` and by the ops CLI.
    """


class SettlementError(PayzenoLedgerError):
    """Base for everything the settlement/reconciliation path can refuse."""

    code: ClassVar[str] = "settlement_failed"
    http_status: ClassVar[int] = 422


class BatchNotReconcilableError(SettlementError):
    """The batch is not in a status a run may start against.

    ``SettlementService.close_batch`` and ``reconciliation.start_run`` both check
    ``RECONCILABLE_BATCH_STATUSES``; an ``open`` batch is still receiving lines.
    """

    code: ClassVar[str] = "batch_not_reconcilable"


class SettlementLockedError(SettlementError):
    """Someone else holds the batch advisory lock.

    409, and since PAY-2043 it is the **normal** outcome of a retry losing a race to a
    running sweep: ``RetryScheduler.retry_item`` returns ``None`` and the route maps that
    onto this. Its rate is a signal to watch, not an error budget to burn — the console
    treats it as "already settling", refetches, and shows no toast.
    """

    code: ClassVar[str] = "settlement_locked"
    http_status: ClassVar[int] = 409


class OrphanedItemError(SettlementError):
    """The reconciliation item has no matched charge.

    Raised in ``SettlementPoster.post_settlement`` on ``item.charge_id is None``,
    **before** ``SettlementChargeRepository.get_or_raise``, so an unmatched acquirer line
    reports itself as unmatched rather than as a missing projection.
    """

    code: ClassVar[str] = "orphaned_item"


class RetryableSettlementError(SettlementError):
    """The acquirer failed in a way that is worth trying again.

    Carries the processor's own ``code`` — both ``ReconciliationService.reconcile_batch``
    and ``RetryScheduler.retry_item`` catch this **by name** and read ``exc.code`` to pick
    the item's next status and its backoff, so a bare re-raise of the underlying
    ``UpstreamError`` loses the only thing the caller needs.
    """

    code: ClassVar[str] = "retryable_settlement"


class SettlementVarianceExceededError(SettlementError):
    """``abs(variance_minor)`` is over the merchant's settlement tolerance.

    A real money difference between what the acquirer says it settled and what we
    expected. Never auto-posted; ``settlement.variance_detected`` goes out and a human
    resolves it through the ops match endpoint.
    """

    code: ClassVar[str] = "settlement_variance_exceeded"


class RetryExhaustedError(SettlementError):
    """``attempt_count`` reached ``Settings.reconcile_max_attempts``.

    Read off ``Settings`` and not off ``constants.MAX_ATTEMPTS`` — the constant is only
    the default. During PAY-2041 the ceiling was changed at 01:44 without a deploy, and
    that is only possible because the read path is the settings object.
    """

    code: ClassVar[str] = "retry_exhausted"


# ---------------------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------------------


class PayoutError(PayzenoLedgerError):
    """Base for every reason a payout will not be created or initiated."""

    code: ClassVar[str] = "payout_blocked"
    http_status: ClassVar[int] = 422


class PayoutBlockedError(PayoutError):
    """The merchant is ``restricted`` or ``suspended``.

    Risk's decision, surfaced as a refusal rather than a silent zero payout so the
    console can say why.
    """


class InsufficientBalanceError(PayoutError):
    """``PayoutCalculator.compute_available()`` came back at or below zero.

    Available is gross balance minus reserves minus in-flight payouts, and the in-flight
    subtraction is why ``create_payout`` takes the merchant/currency advisory lock first.
    """


class BankAccountUnusableError(PayoutError):
    """The destination bank account projection is not ``verified``.

    Raised by every ``PayoutInitiator.initiate``. Each rail checks it itself rather than
    trusting the service, because the rails are also reachable from the ops CLI.
    """


# ---------------------------------------------------------------------------------------
# Everything else
# ---------------------------------------------------------------------------------------


class DualControlRequiredError(PayzenoLedgerError):
    """Maker and checker are the same staff identity, or no identity was forwarded.

    ``AdjustmentService.approve`` compares ``approved_by`` with ``requested_by``. The
    ledger does not authenticate humans — payzeno-api's ``StaffGuard`` did — but it does
    record which one, because dual control is meaningless otherwise.
    """

    code: ClassVar[str] = "permission_denied"
    http_status: ClassVar[int] = 403


class DuplicateDisputeError(PayzenoLedgerError):
    """A ``dispute.opened`` arrived for a charge that already has an open dispute.

    Raised in ``app/consumers/handlers/payments.py``. The consumer's ``_claim_event``
    dedupes by ``event_id``; this catches the case where the *upstream* emitted two
    distinct events for one dispute, which Worldflow does on re-presentment.
    """

    code: ClassVar[str] = "duplicate_dispute"
    http_status: ClassVar[int] = 409


class UpstreamError(PayzenoLedgerError):
    """Something outside this service failed. Nothing here is the ledger's fault."""

    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 502


class ProcessorUnavailableError(UpstreamError):
    """The acquirer returned 5xx, rate-limited us, or the circuit is open.

    When the circuit is open this is raised with **no HTTP call made at all** — see
    ``app/clients/breaker.py``. The ``code`` keyword carries the acquirer's own reason so
    ``RETRYABLE_ERROR_CODES`` can be consulted downstream.
    """

    code: ClassVar[str] = "processor_unavailable"


class ProcessorIndeterminateError(UpstreamError):
    """A read timeout or a connection reset. We do not know what happened.

    Same status and code as :class:`ProcessorUnavailableError` on the wire — the caller
    cannot do anything different — but a *different Python type*, because internally the
    difference is everything: an indeterminate capture may already have charged the
    cardholder, so it routes to ``ProcessorClient.get_capture_status`` instead of being
    re-issued. That split is PAY-2060 and ``INDETERMINATE_ERROR_CODES``.
    """

    code: ClassVar[str] = "processor_unavailable"


class BusPublishError(UpstreamError):
    """SNS refused a publish. Raised only by ``SnsPublisher``, only from the outbox drain.

    The business path publishes through ``OutboxPublisher`` into the same transaction, so
    a bus outage can never fail a settlement — it can only make the drain retry.
    """

    code: ClassVar[str] = "internal_error"
