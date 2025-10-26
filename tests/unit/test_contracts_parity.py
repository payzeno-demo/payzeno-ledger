"""Cross-repo contract test — the Python side of payzeno-contracts' codegen.

tests.md §4: `payzeno_contracts.types` must expose every name `types.ts` exports, and
`payzeno_contracts.events` every name `events.ts` exports. The TypeScript side asserts the
mirror image (`src/codegen/parity.test.ts`). Between them, a contracts release that forgets
to regenerate the Python package fails somebody's CI instead of failing at 3am in a
consumer.

The ledger never redefines a contract type. `app/api/schemas.py` re-exports; the enum tuples
that back our pg enums come from here; and there is no local `EventEnvelope` class anywhere
in `app/`.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from payzeno_contracts import events as contract_events
from payzeno_contracts import types as contract_types

# --- types.ts, the enum tuples app/models/** binds its pg enums to ---------------------
REQUIRED_ENUM_TUPLES = [
    "CURRENCIES",
    "ENTRY_DIRECTIONS",
    "LEDGER_PURPOSES",
    "LEDGER_REFERENCE_TYPES",
    "LEDGER_ACTORS",
    "ACCOUNT_TYPES",
    "ACCOUNT_STATUSES",
    "SETTLEMENT_BATCH_STATUSES",
    "RECONCILIATION_ITEM_STATUSES",
    "RECONCILIATION_RUN_STATUSES",
    "RECONCILIATION_LINE_TYPES",
    "RECONCILIATION_MATCH_METHODS",
    "PAYOUT_STATUSES",
    "PAYOUT_METHODS",
    "PAYOUT_FAILURE_CODES",
    "ACQUIRERS",
    "ERROR_CODES",
    "PRICING_MODELS",
    "RISK_TIERS",
    "MERCHANT_STATUSES",
]

REQUIRED_MODELS = [
    "Money",
    "ApiError",
    "FieldError",
    "Account",
    "Balance",
    "LedgerTransaction",
    "LedgerEntry",
    "SettlementBatch",
    "ReconciliationRun",
    "ReconciliationItem",
    "ReconciliationBacklog",
    "Payout",
    "CreatePayoutRequest",
    "PostTransactionRequest",
    "StartReconciliationRequest",
    "RetryReconciliationItemRequest",
    "BootstrapAccountsResponse",
    "BalanceHistoryResponse",
    "TrialBalanceResult",
    "InvoiceLine",
]

REQUIRED_EVENT_NAMES = [
    "EventEnvelope",
    "PayzenoEvent",
    "EventType",
    "EVENT_TYPES",
    "EVENT_TOPICS",
    "EVENT_SCHEMA_VERSIONS",
    "PaymentAuthorizedPayload",
    "PaymentCapturedPayload",
    "PaymentCanceledPayload",
    "RefundCreatedPayload",
    "DisputeOpenedPayload",
    "DisputeClosedPayload",
    "MerchantCreatedPayload",
    "MerchantUpdatedPayload",
    "MerchantStatusChangedPayload",
    "MerchantBankAccountVerifiedPayload",
    "SettlementBatchClosedPayload",
    "SettlementItemSettledPayload",
    "SettlementCompletedPayload",
    "SettlementReconciliationFailedPayload",
    "SettlementDuplicateDetectedPayload",
    "SettlementVarianceDetectedPayload",
    "SettlementFundedPayload",
]

# Emitted by this service — app/publishers/**, blueprint §6.3.
LEDGER_EMITTED_EVENT_TYPES = [
    "settlement.batch_closed",
    "settlement.item_settled",
    "settlement.completed",
    "settlement.reconciliation_failed",
    "settlement.duplicate_detected",
    "settlement.variance_detected",
    "settlement.funded",
    "payout.scheduled",
    "payout.paid",
    "payout.failed",
    "payout.returned",
    "ledger.transaction_posted",
    "ledger.imbalance_detected",
]

# Consumed by this service — app/consumers/**, blueprint §6.4.
LEDGER_CONSUMED_EVENT_TYPES = [
    "payment.authorized",
    "payment.captured",
    "payment.canceled",
    "refund.created",
    "dispute.opened",
    "dispute.closed",
    "merchant.created",
    "merchant.updated",
    "merchant.status_changed",
    "merchant.bank_account_verified",
]


@pytest.mark.parametrize("name", REQUIRED_ENUM_TUPLES)
def test_types_module_exposes_the_enum_tuple(name: str) -> None:
    assert hasattr(contract_types, name), f"payzeno_contracts.types is missing {name}"
    value = getattr(contract_types, name)
    assert isinstance(value, tuple)
    assert value, f"{name} is empty"
    assert all(isinstance(member, str) for member in value)


@pytest.mark.parametrize("name", REQUIRED_MODELS)
def test_types_module_exposes_the_model(name: str) -> None:
    assert hasattr(contract_types, name), f"payzeno_contracts.types is missing {name}"


@pytest.mark.parametrize("name", REQUIRED_EVENT_NAMES)
def test_events_module_exposes_the_name(name: str) -> None:
    assert hasattr(contract_events, name), f"payzeno_contracts.events is missing {name}"


@pytest.mark.parametrize("event_type", LEDGER_EMITTED_EVENT_TYPES)
def test_every_event_we_emit_is_a_known_event_type(event_type: str) -> None:
    assert event_type in contract_events.EVENT_TYPES


@pytest.mark.parametrize("event_type", LEDGER_CONSUMED_EVENT_TYPES)
def test_every_event_we_consume_is_a_known_event_type(event_type: str) -> None:
    assert event_type in contract_events.EVENT_TYPES


@pytest.mark.parametrize(
    "event_type", LEDGER_EMITTED_EVENT_TYPES + LEDGER_CONSUMED_EVENT_TYPES
)
def test_every_event_we_touch_has_a_topic_and_a_schema_version(event_type: str) -> None:
    # The publishers stamp EventEnvelope.version off EVENT_SCHEMA_VERSIONS. A missing key
    # there means an unversioned envelope on the bus, and PAY-2052 exists precisely because
    # a consumer could not tell v1 from v2 of settlement.completed.
    assert event_type in contract_events.EVENT_TOPICS
    assert event_type in contract_events.EVENT_SCHEMA_VERSIONS


def test_settlement_duplicate_detected_carries_detected_by() -> None:
    """Added by the PR nmigration opened at 02:26 on the night of PAY-2041.

    `detected_by` is populated from `SettlementPoster.post_settlement`'s `caller` argument.
    One shared poster instance serves both the sweep and the retry, so an instance attribute
    could not have told us which path lost the race — which is the whole reason the field
    exists.
    """
    payload = contract_events.SettlementDuplicateDetectedPayload
    fields = set(getattr(payload, "model_fields", {}))
    assert {"detected_by", "idempotency_key", "existing_transaction_id"} <= fields


def test_no_module_under_app_declares_its_own_event_envelope() -> None:
    """interfaces.md §3.1 — `EventEnvelope` is always the contracts one.

    A local envelope class is how two services end up disagreeing about `correlation_id`
    propagation without either of them being wrong on its own.
    """
    import app

    offenders: list[str] = []
    for module_info in pkgutil.walk_packages(app.__path__, prefix="app."):
        module = importlib.import_module(module_info.name)
        declared = getattr(module, "EventEnvelope", None)
        if declared is not None and declared is not contract_events.EventEnvelope:
            offenders.append(module_info.name)

    assert offenders == []
