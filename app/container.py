"""The single construction site.

Every repository, service, client, publisher, consumer and job in this service is
constructed here, exactly once, and handed out by attribute. ``app/api/deps.py`` pulls
instances off the container; it never builds one. Nothing else in ``app/`` calls a
constructor of anything in ``app/services/``.

**Why "exactly once" is load-bearing and not a preference.** There is one
``SettlementPoster`` in the process, and both ``ReconciliationService`` (the 15-minute
sweep) and ``RetryScheduler`` (the 60-second drain, and the HTTP retry route) hold that
same instance. A dependency that built a poster per request would give each caller its
own — which sounds tidier and would have made PAY-2041 impossible to *reproduce*, while
leaving it entirely possible in production, because the thing they actually share is the
database, not the object. The shape of the code should match the shape of the hazard.

The same argument is why ``BaseRepository`` is stateless and takes no session: three
sessions in one ``reconcile_batch`` pass are three transactions against one repository
instance, and a constructor-bound session would collapse them into one.

Layering: L8. This module is allowed to import from every layer; nothing imports it back
except ``app/main.py`` and ``app/api/deps.py`` (under ``TYPE_CHECKING`` only).
"""

from __future__ import annotations

from typing import Any

from app.clients.breaker import BreakerProcessorClient, build_breaker
from app.clients.nordpay import NordpayClient
from app.clients.sandbox import SandboxProcessorClient
from app.clients.worldflow import WorldflowClient
from app.clock import SystemClock
from app.config import Settings
from app.consumers.merchant_events import MerchantEventConsumer
from app.consumers.payment_events import PaymentEventConsumer
from app.consumers.sqs import sqs_client_factory
from app.db.locks import AdvisoryLockManager
from app.db.session import PooledSessionFactory, create_engine
from app.domain.calendar import BankingCalendar
from app.flags import EnvFeatureFlags
from app.logging import get_logger
from app.metrics import metrics
from app.publishers.outbox import OutboxPublisher
from app.publishers.sns import SnsPublisher
from app.repositories.account import AccountRepository
from app.repositories.balance_cache import MerchantBalanceCacheRepository
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
from app.services.accounts import AccountResolver
from app.services.audit import AdjustmentService, LedgerAuditService
from app.services.balances import BalanceService
from app.services.captures import DeferredCaptureService
from app.services.funding import FundingMatchService
from app.services.invoices import InvoiceStagingService
from app.services.payouts import PayoutCalculator, PayoutService
from app.services.rails.ach import AchPayoutInitiator, SameDayAchPayoutInitiator
from app.services.rails.debit_ach import AchPayoutPuller
from app.services.rails.faster_payments import FasterPaymentsPayoutInitiator
from app.services.rails.sepa import SepaPayoutInitiator
from app.services.reconciliation.backlog import BacklogService
from app.services.reconciliation.matcher import (
    ExactReferenceMatch,
    HeuristicAmountWindowMatch,
    ManualMatch,
    NetworkTransactionMatch,
)
from app.services.reconciliation.poster import SettlementPoster
from app.services.reconciliation.reconciler import ReconciliationService
from app.services.reconciliation.retry import RetryScheduler
from app.services.reserves import ReserveService
from app.services.settlements import SettlementImportService, SettlementService
from app.services.transactions import LedgerPoster
from app.workers.batch_close import BatchCloseJob
from app.workers.deferred_capture import DeferredCaptureJob
from app.workers.funding_match import FundingMatchJob
from app.workers.ledger_audit import LedgerAuditJob
from app.workers.negative_balance import NegativeBalanceJob
from app.workers.outbox_drain import OutboxDrainJob
from app.workers.payout_scheduler import PayoutSchedulerJob
from app.workers.reconcile_sweep import ReconciliationSweepJob
from app.workers.reserve_release import ReserveReleaseJob
from app.workers.retry_drain import RetryDrainJob
from app.workers.settlement_import import SettlementImportJob

logger = get_logger(__name__)

__all__ = ["Container", "Repositories", "build_container"]


class Repositories:
    """The repository bundle, constructed once and shared by everything.

    Handed whole to the read-only list routes through ``deps.get_repositories`` — there is
    no business rule between "list transactions for a merchant" and the query, and routing
    that through a service that only forwards is the layer nobody can delete later.

    Every one of these is **stateless**. They take a session per call. See
    ``app/repositories/base.py`` and ``interfaces.md`` §3.2.
    """

    def __init__(self) -> None:
        self.accounts = AccountRepository()
        self.transactions = LedgerTransactionRepository()
        self.entries = LedgerEntryRepository()
        self.balance_cache = MerchantBalanceCacheRepository()
        self.batches = SettlementBatchRepository()
        self.items = ReconciliationItemRepository()
        self.runs = ReconciliationRunRepository()
        self.charges = SettlementChargeRepository()
        self.merchants = MerchantProjectionRepository()
        self.banks = BankAccountProjectionRepository()
        self.payouts = PayoutRepository()
        self.processed_events = ProcessedEventRepository()
        self.outbox = EventOutboxRepository()
        self.fundings = FundingEventRepository()
        self.captures = CaptureAttemptRepository()
        self.reserves = ReserveHoldRepository()
        self.adjustments = LedgerAdjustmentRequestRepository()
        self.invoice_staging = InvoiceLineStagingRepository()


class Container:
    """Everything, wired.

    Construction order follows ``blueprint`` §12 and is dependency-safe top to bottom:
    infrastructure, repositories, clients, publishers, then services innermost-first, then
    consumers and jobs. Nothing here does I/O — the engine is created but not connected
    until the first ``begin()``, so importing and building a container in a test is cheap.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        # -- L-1 / L2 infrastructure -------------------------------------------------
        self.clock = SystemClock()
        self.flags = EnvFeatureFlags(settings)
        self.engine = create_engine(settings)
        self.sessions = PooledSessionFactory(self.engine)
        self.locks = AdvisoryLockManager()

        # -- L3 repositories ---------------------------------------------------------
        self.repositories = Repositories()
        repos = self.repositories

        # -- L4 clients and publishers ----------------------------------------------
        # Both acquirers sit behind ONE breaker facade with a circuit per acquirer, so an
        # open Worldflow circuit never stops a Nordpay settlement. The sandbox client is
        # wired in production, not only in tests: test-mode merchants route to it.
        self.breaker = build_breaker(settings, redis=None)
        self.processor = BreakerProcessorClient(
            clients={
                "worldflow": WorldflowClient(settings),
                "nordpay": NordpayClient(settings),
                "sandbox": SandboxProcessorClient(),
            },
            merchants=repos.merchants,
            metrics=metrics,
            settlements=self.settlement_service,
            calculator=self.payout_calculator,
            sessions=self.sessions,
            settings=settings,
        )

    async def aclose(self) -> None:
        """Dispose the engine. Called from ``create_app``'s shutdown hook."""
        await self.sessions.dispose()

    def describe(self) -> dict[str, Any]:
        """A flat summary for ``GET /readyz`` and for the startup log.

        Deliberately includes the pool gauges: ``checked_out`` climbing to ``pool_size``
        while a sweep runs is the signature of a drain convoying behind a batch lock
        (PAY-2057), and having it on the readiness payload means the first person to look
        already has it.
        """
        return {
            "pool": self.sessions.pool_status(),
            "retry_drain_enabled": self.settings.retry_drain_enabled,
            "flags": repr(self.flags),
        }


def build_container(settings: Settings) -> Container:
    """Construct the one container. Called from ``create_app`` and from ``app/ops/cli.py``.

    A function rather than a module-level instance so importing ``app.container`` does not
    create an engine — which matters for Alembic, for ``--help`` on the CLI, and for every
    test that imports a service class for its type.
    """
    container = Container(settings)
    logger.info(
        "container_built",
        pool_size=settings.database_pool_size,
        retry_drain_enabled=settings.retry_drain_enabled,
    )
    return container
