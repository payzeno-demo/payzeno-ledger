"""Data access — layer L3, one repository per aggregate.

Every class here is **stateless**: no session on the instance, no cached row, no unit of
work. `app/container.py` constructs each of them exactly once with no arguments and hands
the same instance to every service, worker and consumer. The session comes down the call
as the first parameter.

That is not an aesthetic. ``ReconciliationService.reconcile_batch`` runs three sessions in
one pass against one shared ``ReconciliationItemRepository``, and ``RetryScheduler.drain``
a fourth — see `docs/adr/0011-lock-ordering-in-the-money-path.md`. A repository that held
a session would quietly make those four transactions one transaction.

Repositories never commit and never open a transaction. The service layer owns both.
"""

from app.repositories.account import AccountRepository
from app.repositories.balance_cache import MerchantBalanceCacheRepository
from app.repositories.base import BaseRepository, Page
from app.repositories.capture_attempt import CaptureAttemptRepository
from app.repositories.funding_event import FundingEventRepository
from app.repositories.invoice_staging import InvoiceLineStagingRepository
from app.repositories.ledger_adjustment import LedgerAdjustmentRequestRepository
from app.repositories.ledger_entry import LedgerEntryRepository
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.repositories.outbox import EventOutboxRepository
from app.repositories.payout import PayoutRepository
from app.repositories.processed_event import ProcessedEventRepository
from app.repositories.projections import (
    BankAccountProjectionRepository,
    MerchantProjectionRepository,
)
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.reconciliation_run import ReconciliationRunRepository
from app.repositories.reserve_hold import ReserveHoldRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.repositories.settlement_charge import SettlementChargeRepository

__all__ = [
    "AccountRepository",
    "BankAccountProjectionRepository",
    "BaseRepository",
    "CaptureAttemptRepository",
    "EventOutboxRepository",
    "FundingEventRepository",
    "InvoiceLineStagingRepository",
    "LedgerAdjustmentRequestRepository",
    "LedgerEntryRepository",
    "LedgerTransactionRepository",
    "MerchantBalanceCacheRepository",
    "MerchantProjectionRepository",
    "Page",
    "PayoutRepository",
    "ProcessedEventRepository",
    "ReconciliationItemRepository",
    "ReconciliationRunRepository",
    "ReserveHoldRepository",
    "SettlementBatchRepository",
    "SettlementChargeRepository",
]
