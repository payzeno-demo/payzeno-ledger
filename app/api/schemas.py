"""The request/response surface of ``/internal/v1``.

**The ledger does not define contract types.** Everything payzeno-api, payzeno-console or
payzeno-billing-legacy can see is generated from ``payzeno-contracts`` and re-exported
here, so every ``response_model=`` in ``app/api/routers/*`` resolves through one module.
When ``pnpm run codegen:python`` bumps ``payzeno-contracts``, the ledger's HTTP surface
moves with it or the build fails — which is the entire point of the package.

What *is* defined locally: the handful of internal request bodies that have no public
equivalent, because no console screen and no merchant API exposes them. ``ops`` bodies,
the legacy import envelope and the funding notification are all of that kind. Each is a
plain pydantic model with no counterpart in ``types.ts``, and adding one there instead
would put staff-only shapes in the merchant SDK.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, Literal

from payzeno_contracts.types import (
    Account,
    AccountType,
    Acquirer,
    ApiError,
    Balance,
    BalanceBucket,
    BalanceHistoryResponse,
    BootstrapAccountsResponse,
    ChargeLedgerResponse,
    CreatePayoutRequest,
    CurrencyCode,
    EntryDirection,
    FieldError,
    FundingEvent,
    HealthResponse,
    InvoiceLine,
    LedgerAdjustmentRequest,
    LedgerEntry,
    LedgerPurpose,
    LedgerTransaction,
    MerchantSettlementBatch,
    Paginated,
    Payout,
    PayoutMethod,
    PostTransactionLine,
    PostTransactionRequest,
    ReadinessResponse,
    ReconciliationBacklog,
    ReconciliationItem,
    ReconciliationRun,
    RetryReconciliationItemRequest,
    SettlementBatch,
    StartReconciliationRequest,
    TimeseriesInterval,
    TrialBalanceResult,
)
from pydantic import BaseModel, Field

__all__ = [
    # ---- re-exported from payzeno_contracts.types ----
    "Account",
    "AccountType",
    "Acquirer",
    "ApiError",
    "Balance",
    "BalanceBucket",
    "BalanceHistoryResponse",
    "BootstrapAccountsResponse",
    "ChargeLedgerResponse",
    "CreatePayoutRequest",
    "CurrencyCode",
    "EntryDirection",
    "FieldError",
    "FundingEvent",
    "HealthResponse",
    "InvoiceLine",
    "LedgerAdjustmentRequest",
    "LedgerEntry",
    "LedgerPurpose",
    "LedgerTransaction",
    "MerchantSettlementBatch",
    "Paginated",
    "Payout",
    "PayoutMethod",
    "PostTransactionLine",
    "PostTransactionRequest",
    "ReadinessResponse",
    "ReconciliationBacklog",
    "ReconciliationItem",
    "ReconciliationRun",
    "RetryReconciliationItemRequest",
    "SettlementBatch",
    "StartReconciliationRequest",
    "TimeseriesInterval",
    "TrialBalanceResult",
    # ---- ledger-internal only ----
    "AccountListResponse",
    "ApproveAdjustmentRequest",
    "BalanceListResponse",
    "BootstrapAccountsRequest",
    "BulkPostTransactionRequest",
    "BulkPostTransactionResponse",
    "FreezeAccountRequest",
    "JobRunResponse",
    "LegacySettlementRecord",
    "ManualMatchRequest",
    "MarkPayoutFailedRequest",
    "MarkPayoutPaidRequest",
    "OpenSettlementBatchRequest",
    "RecordFundingRequest",
    "RequestAdjustmentRequest",
    "ReverseTransactionRequest",
    "RunTrialBalanceRequest",
    "SettlementImportRequest",
    "SettlementImportResponse",
    "StageInvoiceLinesRequest",
    "StageInvoiceLinesResponse",
    "StagedInvoiceLinesResponse",
    # ---- helpers ----
    "contract_page",
    "contract_payload",
]


class BootstrapAccountsRequest(BaseModel):
    """``POST /internal/v1/accounts/bootstrap``.

    payzeno-api calls this once per merchant per currency, right after
    ``merchant.created``. It is idempotent by construction — ``AccountResolver`` upserts
    on ``uq_account_merchant_type_currency_livemode``.
    """

    merchant_id: str
    currency: str
    livemode: bool = True


class FreezeAccountRequest(BaseModel):
    """``POST /internal/v1/accounts/{account_id}/freeze``. Risk-initiated, always."""

    reason: str = Field(min_length=1, max_length=500)


class AccountListResponse(BaseModel):
    """``GET /internal/v1/accounts``. Unpaginated — a merchant has at most a dozen."""

    data: list[Account]


class BalanceListResponse(BaseModel):
    """``GET /internal/v1/balances/{merchant}`` without ``?currency=``: one per currency.

    Unpaginated for the same reason as :class:`AccountListResponse` — five currencies at
    most. The same envelope, so a caller reads both with one helper.
    """

    data: list[Balance]


class JobRunResponse(BaseModel):
    """``POST /internal/v1/ops/jobs/{name}/run`` — the job's ``JobResult``, verbatim.

    ``error`` is set when the tick failed; the HTTP status is still 200, because the
    request (run the job once) succeeded and the job's own failure is the answer.
    """

    name: str
    items_processed: int
    duration_ms: int
    error: str | None


class ReverseTransactionRequest(BaseModel):
    """``POST /internal/v1/transactions/{transaction_id}/reverse``.

    The caller supplies the idempotency key: a reversal issued twice because payzeno-api
    retried its own HTTP call must not produce two reversals.
    """

    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=255)


class BulkPostTransactionRequest(BaseModel):
    """``POST /internal/v1/transactions/bulk``.

    Added in month 2 for a backfill that has since finished. Nothing has called it since
    month 5; see the route in ``app/api/routers/transactions.py``.
    """

    transactions: list[PostTransactionRequest]


class BulkPostTransactionResponse(BaseModel):
    posted: int
    transaction_ids: list[str]


class OpenSettlementBatchRequest(BaseModel):
    """``POST /internal/v1/settlement-batches``. Sole caller: ``SettlementImportService``."""

    acquirer: str
    currency: str
    processing_date: date
    file_reference: str
    livemode: bool = True


class RecordFundingRequest(BaseModel):
    """``POST /internal/v1/settlement-batches/{batch_id}/funding``.

    Treasury posts this when the bank credit lands. It is the only proof money actually
    arrived — a closed, fully-reconciled batch is still unfunded until this fires.
    """

    bank_reference: str = Field(min_length=1, max_length=255)
    amount_minor: int
    value_date: date


class MarkPayoutPaidRequest(BaseModel):
    paid_at: datetime
    bank_reference: str = Field(min_length=1, max_length=255)


class MarkPayoutFailedRequest(BaseModel):
    failure_code: str
    failure_message: str = Field(max_length=1000)


class RunTrialBalanceRequest(BaseModel):
    """``POST /internal/v1/ops/audit/trial-balance``. Staff-only, not in ``types.ts``."""

    currency: str
    as_of: datetime | None = None


class RequestAdjustmentRequest(BaseModel):
    """``POST /internal/v1/ops/adjustments`` — the maker half of maker-checker."""

    merchant_id: str | None = None
    currency: str
    lines: list[PostTransactionLine]
    reason_code: str = Field(min_length=1, max_length=64)


class ApproveAdjustmentRequest(BaseModel):
    """``POST /internal/v1/ops/adjustments/{request_id}/approve`` — the checker half."""

    approver_note: str = Field(min_length=1, max_length=1000)


class ManualMatchRequest(BaseModel):
    """``POST /internal/v1/ops/items/{item_id}/match``.

    Routes to the ``ManualMatch`` strategy, which is the only one that will attach a
    charge the automatic strategies rejected.
    """

    charge_id: str
    note: str = Field(max_length=1000, default="")


class StageInvoiceLinesRequest(BaseModel):
    """``POST /internal/v1/invoices/lines/stage`` — arc MIG step 4 dual-write."""

    merchant_id: str
    invoice_public_id: str
    period_start: date
    period_end: date
    currency: str
    lines: list[InvoiceLine]


class StageInvoiceLinesResponse(BaseModel):
    staged: int


class StagedInvoiceLinesResponse(BaseModel):
    lines: list[dict[str, Any]]


class LegacySettlementRecord(BaseModel):
    """One row of the Java service's settlement export.

    Field names are the Java DTO's, not ours: ``LedgerReconciliationExportJob`` serialises
    ``SettlementRecord`` straight onto the wire and renaming them here would mean
    changing a Spring service to suit a Python one.
    """

    acquirer_reference: str
    network_transaction_id: str | None = None
    line_type: str
    gross_minor: int
    fee_minor: int = 0
    net_minor: int
    interchange_minor: int = 0
    scheme_fee_minor: int = 0
    currency: str
    merchant_id: str | None = None
    charge_id: str | None = None
    posted_at: datetime | None = None


class SettlementImportRequest(BaseModel):
    """``POST /internal/v1/settlement-imports``. Called by payzeno-billing-legacy only."""

    acquirer: str
    processing_date: date
    file_reference: str
    records: list[LegacySettlementRecord]


class SettlementImportResponse(BaseModel):
    batch_id: str
    item_count: int


#: Statuses the settlement-item list endpoint will filter on. Restated as a Literal so
#: FastAPI rejects a typo in the query string instead of quietly returning zero rows —
#: which is what ``?status=retriable`` did for a whole afternoon in month 6.
ItemStatusFilter = Literal[
    "pending", "settling", "retryable", "settled", "failed", "orphaned", "needs_review"
]


def contract_payload(model: type[BaseModel], values: Mapping[str, Any]) -> dict[str, Any]:
    """Project a router's values onto exactly one contract model's wire fields.

    The contract models forbid extra keys and type every date and timestamp as ``str``, so
    FastAPI's response validation answers 500 for a column the contract does not carry or
    for a raw ``datetime``. This keeps the model's fields under their wire names
    (``object``, not ``object_``), drops everything else, and renders dates and datetimes
    as ISO-8601. A field the router did not supply is left out, so a genuinely missing
    value still fails validation rather than being invented here.
    """
    payload: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        wire = field.alias or name
        if wire in values:
            payload[wire] = _wire_value(values[wire])
    return payload


def contract_page(
    model: type[BaseModel],
    rows: Iterable[Mapping[str, Any]],
    *,
    has_more: bool,
    next_cursor: str | None,
) -> dict[str, Any]:
    """A ``Paginated`` body: ``data``, ``next_cursor``, ``has_more`` and nothing else."""
    return {
        "data": [contract_payload(model, row) for row in rows],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def _wire_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    return value
