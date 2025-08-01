"""SQLAlchemy declarative models — 20 tables, layer L1.

Every index in `data-model.md` §3 is restated in the owning model's ``__table_args__`` so
the model and the migration cannot drift: an Alembic autogenerate diff against a healthy
schema is empty, and a hand-written migration that forgets an index shows up as a diff in
``make migrate-check``.

Importing this package imports every model, which is what ``app/db/base.py`` relies on to
populate ``Base.metadata`` for Alembic's ``target_metadata``.
"""

from app.models.account import Account
from app.models.base import Base, LivemodeMixin, TimestampMixin, metadata
from app.models.capture_attempt import CaptureAttempt
from app.models.events import EventOutbox, ProcessedEvent
from app.models.funding_event import FundingEvent
from app.models.invoice_line_staging import InvoiceLineStaging
from app.models.ledger_adjustment_request import LedgerAdjustmentRequest
from app.models.ledger_entry import LedgerEntry
from app.models.ledger_transaction import LedgerTransaction, SettlementDuplicateAudit
from app.models.merchant_balance_cache import MerchantBalanceCache
from app.models.payout import Payout
from app.models.projections import (
    BankAccountProjection,
    MerchantProjection,
    SettlementCharge,
)
from app.models.reconciliation_item import ReconciliationItem
from app.models.reconciliation_run import ReconciliationRun
from app.models.reserve import BankingCalendarDay, ReserveHold
from app.models.settlement_batch import SettlementBatch

__all__ = [
    "Account",
    "BankAccountProjection",
    "BankingCalendarDay",
    "Base",
    "CaptureAttempt",
    "EventOutbox",
    "FundingEvent",
    "InvoiceLineStaging",
    "LedgerAdjustmentRequest",
    "LedgerEntry",
    "LedgerTransaction",
    "LivemodeMixin",
    "MerchantBalanceCache",
    "MerchantProjection",
    "Payout",
    "ProcessedEvent",
    "ReconciliationItem",
    "ReconciliationRun",
    "ReserveHold",
    "SettlementBatch",
    "SettlementCharge",
    "SettlementDuplicateAudit",
    "TimestampMixin",
    "metadata",
]
