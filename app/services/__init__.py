"""Business logic — the layer that owns transactions.

Everything below this package (repositories, db, models, domain) is transaction-unaware;
everything above it (api, workers, consumers) does not open one. Services are the
boundary: they either take a session as a per-call argument, or they hold a
``SessionFactory`` and open one per unit of work. Never both for the same method.

All services are constructed once in ``app/container.py`` and shared across concurrent
requests, jobs and consumers, so none of them may hold mutable per-request state.
"""

from app.services.accounts import AccountResolver
from app.services.audit import AdjustmentService, LedgerAuditService, TrialBalanceResult
from app.services.balances import BalanceService
from app.services.captures import DeferredCaptureService
from app.services.funding import FundingMatchService
from app.services.invoices import InvoiceStagingService
from app.services.payouts import PayoutCalculator, PayoutInitiator, PayoutService
from app.services.reserves import ReserveService
from app.services.settlement_parser import (
    LegacyFixedWidthParser,
    SettlementFileParser,
    WorldflowCsvParser,
)
from app.services.settlements import SettlementImportService, SettlementService
from app.services.transactions import IdempotencyClaim, LedgerPoster, PostResult

__all__ = [
    "AccountResolver",
    "AdjustmentService",
    "BalanceService",
    "DeferredCaptureService",
    "FundingMatchService",
    "IdempotencyClaim",
    "InvoiceStagingService",
    "LedgerAuditService",
    "LedgerPoster",
    "LegacyFixedWidthParser",
    "PayoutCalculator",
    "PayoutInitiator",
    "PayoutService",
    "PostResult",
    "ReserveService",
    "SettlementFileParser",
    "SettlementImportService",
    "SettlementService",
    "TrialBalanceResult",
    "WorldflowCsvParser",
]
