"""FastAPI dependencies — the seam between HTTP and the service layer.

Two jobs and no others:

1. **Auth.** ``require_internal_service`` makes the ``internal`` marker from
   ``api-surface.md`` §1.2 visible on every handler signature. It duplicates what
   :class:`~app.middleware.internal_auth.InternalAuthMiddleware` already enforced, on
   purpose: the middleware is the enforcement, the dependency is the documentation, and
   a route that forgets the dependency is still closed.
2. **Resolution.** Every service and repository is constructed exactly once, in
   ``app/container.py``, and stashed on ``app.state.container`` by ``create_app``. The
   getters below pull instances off it. They construct nothing — a dependency that
   builds a service per request would give each request its own ``SettlementPoster``,
   and the whole reason arc INC is possible is that there is exactly one.

``app.container`` is imported under ``TYPE_CHECKING`` only. It sits at L8 and this module
at L7, and importing upward is a review rejection (ADR 0002).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, Query, Request

from app.errors import DualControlRequiredError, ValidationError
from app.repositories.base import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

if TYPE_CHECKING:  # pragma: no cover
    from app.container import Container
    from app.db.locks import AdvisoryLockManager
    from app.ports import SessionFactory
    from app.services.audit import AdjustmentService, LedgerAuditService
    from app.services.balances import BalanceService
    from app.services.funding import FundingMatchService
    from app.services.invoices import InvoiceStagingService
    from app.services.payouts import PayoutService
    from app.services.reconciliation.backlog import BacklogService
    from app.services.reconciliation.matcher import ManualMatch
    from app.services.reconciliation.reconciler import ReconciliationService
    from app.services.reconciliation.retry import RetryScheduler
    from app.services.settlements import SettlementService
    from app.services.transactions import LedgerPoster

#: Header the internal caller must send. Checked properly by the middleware; read here
#: so the handler can attribute the call in an audit row.
SERVICE_HEADER = "X-Payzeno-Service"

#: Staff identity, forwarded by payzeno-api from the operator's ``staff_session``. The
#: ledger does not authenticate humans — it trusts payzeno-api to have done it — but it
#: does record *which* human, because dual control on adjustments is meaningless
#: otherwise.
STAFF_HEADER = "X-Payzeno-Staff-Id"


def get_container(request: Request) -> "Container":
    """The one DI container, built in ``create_app`` and never rebuilt."""
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - only reachable on a broken app factory
        raise RuntimeError("application container is not initialised")
    return container


ContainerDep = Annotated["Container", Depends(get_container)]


def require_internal_service(request: Request) -> str:
    """Assert the caller is a known internal service and return its name.

    The middleware has already rejected anything without a valid
    ``X-Payzeno-Internal-Secret``; by the time a handler runs, the header is present.
    The value is returned rather than discarded so handlers can pass ``created_by`` down
    to the ledger without inventing a second source for it.
    """
    service = request.headers.get(SERVICE_HEADER)
    if not service:
        raise ValidationError(
            "internal service header is required", header=SERVICE_HEADER
        )
    request.state.calling_service = service
    return service


InternalCaller = Annotated[str, Depends(require_internal_service)]


def require_staff_claim(request: Request) -> str:
    """Assert a staff identity was forwarded, and return it.

    Applied to the four ``/internal/v1/ops/*`` routes. It is *not* an authorisation
    check — payzeno-api's ``StaffGuard`` did that — it is an attribution check, and the
    value it returns lands in ``ledger_adjustment_request.requested_by`` /
    ``approved_by``. Without it ``AdjustmentService.approve`` cannot tell maker from
    checker and ``DualControlRequiredError`` has nothing to compare.
    """
    staff_id = request.headers.get(STAFF_HEADER, "").strip()
    if not staff_id:
        raise DualControlRequiredError(
            "a staff identity is required for ops endpoints", header=STAFF_HEADER
        )
    request.state.staff_id = staff_id
    return staff_id


StaffCaller = Annotated[str, Depends(require_staff_claim)]


def page_limit(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> int:
    """Clamp ``?limit=``. ``MAX_PAGE_SIZE`` is 200 and the ops CLI asks for 500."""
    return min(limit, MAX_PAGE_SIZE)


PageLimit = Annotated[int, Depends(page_limit)]


def _pull(container: "Container", attribute: str) -> Any:
    """Read one attribute off the container with a legible failure.

    ``getattr`` on a missing service otherwise surfaces as ``AttributeError: 'Container'
    object has no attribute 'backlog_service'`` from inside FastAPI's dependency solver,
    three frames from anything that names the route.
    """
    instance = getattr(container, attribute, None)
    if instance is None:
        raise RuntimeError(f"container has no {attribute!r}; check app/container.py")
    return instance


def get_sessions(container: ContainerDep) -> "SessionFactory":
    """The pooled session factory. Routes that own a transaction open it themselves."""
    return _pull(container, "sessions")


def get_locks(container: ContainerDep) -> "AdvisoryLockManager":
    return _pull(container, "locks")


def get_ledger_poster(container: ContainerDep) -> "LedgerPoster":
    return _pull(container, "ledger_poster")


def get_account_resolver(container: ContainerDep) -> Any:
    return _pull(container, "account_resolver")


def get_balance_service(container: ContainerDep) -> "BalanceService":
    return _pull(container, "balance_service")


def get_settlement_service(container: ContainerDep) -> "SettlementService":
    return _pull(container, "settlement_service")


def get_reconciliation_service(container: ContainerDep) -> "ReconciliationService":
    return _pull(container, "reconciliation_service")


def get_retry_scheduler(container: ContainerDep) -> "RetryScheduler":
    """The single ``RetryScheduler`` — shared with ``RetryDrainJob``.

    Both the HTTP route and the 60s drain reach the same instance, and through it the
    same ``SettlementPoster``. ``the-incident.md`` §3 depends on that sharing.
    """
    return _pull(container, "retry_scheduler")


def get_backlog_service(container: ContainerDep) -> "BacklogService":
    return _pull(container, "backlog_service")


def get_payout_service(container: ContainerDep) -> "PayoutService":
    return _pull(container, "payout_service")


def get_funding_service(container: ContainerDep) -> "FundingMatchService":
    return _pull(container, "funding_service")


def get_invoice_service(container: ContainerDep) -> "InvoiceStagingService":
    return _pull(container, "invoice_service")


def get_audit_service(container: ContainerDep) -> "LedgerAuditService":
    return _pull(container, "audit_service")


def get_adjustment_service(container: ContainerDep) -> "AdjustmentService":
    return _pull(container, "adjustment_service")


def get_manual_match(container: ContainerDep) -> "ManualMatch":
    return _pull(container, "manual_match")


def get_repositories(container: ContainerDep) -> Any:
    """The repository bundle, for the read-only list routes.

    Read routes go straight to a repository rather than through a service. There is no
    business rule between "list transactions for a merchant" and the query, and routing
    it through a service that only forwards is the kind of layer nobody can delete later.
    """
    return _pull(container, "repositories")
