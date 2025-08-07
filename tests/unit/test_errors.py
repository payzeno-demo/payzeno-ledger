"""The exception tree — app/errors.py.

Every class carries a `code` and an `http_status`, and `app/api/error_handlers.py` is the
only thing that turns them into a response. This module pins the whole table, because the
console branches on `error.code` and payzeno-api's `LedgerHttpClient` branches on the status.

Two rows in particular are not obvious and are asserted individually:

* `AccountFrozenError` is 409, not 500. A frozen account is a deliberate business state set
  through `POST /internal/v1/accounts/:accountId/freeze`. Returning 500 burns the error
  budget, trips payzeno-api's breaker, and pages a human for a policy decision.
* `SettlementLockedError` is 409 and is the NORMAL outcome of a retry losing a race to a
  sweep since PAY-2043. Its rate is a signal, not an error-budget burn.
"""

from __future__ import annotations

import pytest

from app.errors import (
    AccountFrozenError,
    AccountNotFoundError,
    BankAccountProjectionNotFoundError,
    BankAccountUnusableError,
    BatchNotFoundError,
    BatchNotReconcilableError,
    BusPublishError,
    ChargeProjectionNotFoundError,
    CurrencyMismatchError,
    DualControlRequiredError,
    DuplicateDisputeError,
    DuplicateSettlementError,
    IdempotencyConflictError,
    InsufficientBalanceError,
    LedgerIntegrityError,
    LivemodeMismatchError,
    NegativeAmountError,
    NotFoundError,
    OrphanedItemError,
    PayoutBlockedError,
    PayoutError,
    PayzenoLedgerError,
    ProcessorIndeterminateError,
    ProcessorUnavailableError,
    ReconciliationItemNotFoundError,
    RetryableSettlementError,
    RetryExhaustedError,
    SettlementError,
    SettlementLockedError,
    SettlementVarianceExceededError,
    TransactionNotFoundError,
    UnbalancedTransactionError,
    UpstreamError,
    ValidationError,
)

STATUS_TABLE: list[tuple[type[PayzenoLedgerError], int, str]] = [
    (ValidationError, 422, "validation_failed"),
    (NotFoundError, 404, "not_found"),
    (AccountNotFoundError, 404, "not_found"),
    (TransactionNotFoundError, 404, "not_found"),
    (BatchNotFoundError, 404, "not_found"),
    (ReconciliationItemNotFoundError, 404, "not_found"),
    (ChargeProjectionNotFoundError, 404, "not_found"),
    (BankAccountProjectionNotFoundError, 404, "not_found"),
    (LedgerIntegrityError, 500, "ledger_imbalance"),
    (UnbalancedTransactionError, 500, "ledger_imbalance"),
    (NegativeAmountError, 500, "ledger_imbalance"),
    (LivemodeMismatchError, 500, "ledger_imbalance"),
    (CurrencyMismatchError, 400, "currency_mismatch"),
    (AccountFrozenError, 409, "account_frozen"),
    (IdempotencyConflictError, 409, "duplicate_settlement"),
    (DuplicateSettlementError, 409, "duplicate_settlement"),
    (BatchNotReconcilableError, 422, "batch_not_reconcilable"),
    (SettlementLockedError, 409, "settlement_locked"),
    (OrphanedItemError, 422, "orphaned_item"),
    (RetryableSettlementError, 422, "retryable_settlement"),
    (SettlementVarianceExceededError, 422, "settlement_variance_exceeded"),
    (RetryExhaustedError, 422, "retry_exhausted"),
    (PayoutError, 422, "payout_blocked"),
    (PayoutBlockedError, 422, "payout_blocked"),
    (InsufficientBalanceError, 422, "payout_blocked"),
    (BankAccountUnusableError, 422, "payout_blocked"),
    (DualControlRequiredError, 403, "permission_denied"),
    (DuplicateDisputeError, 409, "duplicate_dispute"),
    (ProcessorUnavailableError, 502, "processor_unavailable"),
    (ProcessorIndeterminateError, 502, "processor_unavailable"),
    (BusPublishError, 502, "internal_error"),
]


@pytest.mark.parametrize(("cls", "status", "code"), STATUS_TABLE, ids=lambda v: getattr(v, "__name__", v))
def test_class_maps_to_its_documented_status_and_code(
    cls: type[PayzenoLedgerError], status: int, code: str
) -> None:
    assert cls.http_status == status
    assert cls.code == code


@pytest.mark.parametrize("cls", [row[0] for row in STATUS_TABLE], ids=lambda c: c.__name__)
def test_everything_descends_from_the_one_base(cls: type[PayzenoLedgerError]) -> None:
    assert issubclass(cls, PayzenoLedgerError)
    assert issubclass(cls, Exception)


def test_the_documented_subtree_shape() -> None:
    assert issubclass(AccountNotFoundError, NotFoundError)
    assert issubclass(UnbalancedTransactionError, LedgerIntegrityError)
    assert issubclass(CurrencyMismatchError, LedgerIntegrityError)
    assert issubclass(DuplicateSettlementError, IdempotencyConflictError)
    assert issubclass(OrphanedItemError, SettlementError)
    assert issubclass(RetryableSettlementError, SettlementError)
    assert issubclass(RetryExhaustedError, SettlementError)
    assert issubclass(SettlementLockedError, SettlementError)
    assert issubclass(InsufficientBalanceError, PayoutError)
    assert issubclass(ProcessorUnavailableError, UpstreamError)
    assert issubclass(ProcessorIndeterminateError, UpstreamError)


def test_account_frozen_is_409_and_not_a_server_fault() -> None:
    assert AccountFrozenError.http_status == 409
    assert not issubclass(AccountFrozenError, LedgerIntegrityError)


def test_ledger_imbalance_really_is_a_500() -> None:
    assert LedgerIntegrityError.http_status == 500


def test_retryable_settlement_error_carries_the_processor_code() -> None:
    # reconciler.py and retry.py both catch this by name and read exc.code to decide the
    # item's next status and its backoff. A bare re-raise loses that.
    exc = RetryableSettlementError(item_id="ri_1", code="processor_unavailable")
    assert exc.code == "processor_unavailable"
    assert exc.details["item_id"] == "ri_1"


def test_details_is_always_a_dict() -> None:
    assert PayzenoLedgerError("boom").details == {}
    assert OrphanedItemError(item_id="ri_1").details == {"item_id": "ri_1"}


def test_duplicate_settlement_carries_the_existing_transaction_id() -> None:
    # api-surface.md §10.2 — the 409 body must tell the caller which row won.
    exc = DuplicateSettlementError(existing_transaction_id="txn_1")
    assert exc.details["existing_transaction_id"] == "txn_1"


def test_str_includes_the_code() -> None:
    assert "settlement_locked" in str(SettlementLockedError(item_id="ri_1"))
