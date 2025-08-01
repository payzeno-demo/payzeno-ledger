"""The six seams this service is allowed to have, and the three value types they exchange.

Everything in ``app/services/`` depends on a name in this file rather than on a concrete
class. That is not architecture for its own sake — each of these six has a second
implementation that is *wired in production code*, not only in tests:

===================  =================================  ==================================
Protocol             Production                          Second implementation
===================  =================================  ==================================
``ProcessorClient``  ``WorldflowClient``/``NordpayClient``  ``SandboxProcessorClient``
``EventPublisher``   ``OutboxPublisher``                 ``SnsPublisher`` (drain only)
``Clock``            ``SystemClock``                     ``FrozenClock`` (tests)
``FeatureFlags``     ``EnvFeatureFlags``                 ``StaticFeatureFlags``
``SessionFactory``   ``PooledSessionFactory``            ``SingleConnectionSessionFactory``
``CircuitBreaker``   ``RedisCircuitBreaker``             ``InMemoryCircuitBreaker``
===================  =================================  ==================================

Concretes declare the protocol as an explicit base — ``class WorldflowClient(ProcessorClient)``
— even though structural typing would not require it. The explicit base is what makes the
relationship greppable, and it makes ``mypy --strict`` complain at the definition site
instead of at the thirtieth call site.

Layering: L-1. Imports nothing from ``app/``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, AsyncContextManager, Literal, Protocol, TypeVar, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CaptureResponse",
    "CaptureStatus",
    "CircuitBreaker",
    "Clock",
    "EventPublisher",
    "FeatureFlags",
    "InitiationResult",
    "ProcessorClient",
    "SessionFactory",
]

T = TypeVar("T")


# ---------------------------------------------------------------------------------------
# Value types crossing the acquirer and rail seams
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureResponse:
    """What an acquirer says when a deferred capture succeeds.

    ``reference`` is the acquirer's own handle, not ours. It is stored on
    ``capture_attempt.acquirer_reference`` so a support ticket that quotes the acquirer's
    reference can be resolved without a vendor portal login.
    """

    captured: bool
    reference: str
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    """The answer to "did the cardholder actually get charged?".

    ``unknown`` is a real, expected answer and not an error: some acquirers garbage
    collect idempotency keys after 24h. ``DeferredCaptureJob`` treats ``unknown`` as
    still-pending until the attempt ages out, and never re-issues on the strength of it —
    re-issuing on ``unknown`` is how the second double-charge mechanism (PAY-2060) fires.
    """

    state: Literal["captured", "not_captured", "unknown"]
    reference: str | None


@dataclass(frozen=True, slots=True)
class InitiationResult:
    """What a ``PayoutInitiator`` returns once the rail has accepted a payout.

    ``arrival_estimate`` is a business promise the console shows the merchant, computed
    from the rail's cutoff and ``BankingCalendar`` — it is not the rail's own estimate,
    because two of the four rails do not give one.
    """

    rail_reference: str
    arrival_estimate: date
    submitted_at: datetime


# ---------------------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------------------


@runtime_checkable
class ProcessorClient(Protocol):
    """The acquirer seam. Four methods, and two of them move real money.

    ``confirm_settlement`` is called unconditionally for every reconciliation item;
    ``capture_deferred`` only for ``capture_at_settlement`` charges, and it is the one
    call in this service that reaches a cardholder's card.
    """

    async def confirm_settlement(
        self, acquirer: str, acquirer_reference: str, batch_id: str
    ) -> None:
        """Acknowledge receipt of one settlement line.

        Fails for every item during an acquirer degradation, which is why an outage
        turns the whole batch retryable and not just the deferred-capture merchants.
        """
        ...

    async def capture_deferred(
        self,
        charge_id: str,
        amount_minor: int,
        currency: str,
        reference: str,
        *,
        idempotency_key: str,
    ) -> CaptureResponse:
        """Charge the cardholder for a ``capture_at_settlement`` merchant.

        ``idempotency_key`` is derived from the business fact — ``ledger_key("capture",
        batch_id, charge_id)`` — and never from an attempt counter, so a repeat is
        recognisable by both acquirers.
        """
        ...

    async def get_capture_status(self, acquirer: str, idempotency_key: str) -> CaptureStatus:
        """Ask whether a capture with this key already happened.

        The resolution path for ``INDETERMINATE_ERROR_CODES``. A timeout on a capture is
        the one state where re-issuing is unsafe, so this is asked first.
        """
        ...

    async def fetch_settlement_file(self, acquirer: str, processing_date: date) -> bytes:
        """Pull one day's raw settlement file. CSV for Worldflow, fixed-width for Nordpay."""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """The outbound bus seam. Returns the envelope id it staged or sent.

    ``correlation_id`` is required rather than optional on purpose: a publish with no
    correlation is a trace that dies at the ledger boundary, and every caller has one
    available — the item id, the payout id, or the request's own contextvar.
    """

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        merchant_id: str | None,
        correlation_id: str,
        causation_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Publish one event.

        ``OutboxPublisher`` additionally requires ``session=`` — it stages into the
        caller's transaction so a rolled-back settlement emits nothing — and takes it
        through ``**kwargs`` here so the two implementations stay substitutable.
        """
        ...


@runtime_checkable
class Clock(Protocol):
    """Time, injected.

    Nothing in ``app/`` calls ``datetime.now()`` directly except ``SystemClock``. Domain
    logic that reaches for the wall clock is domain logic that cannot be tested against a
    banking calendar, a cutoff, or a backoff schedule.
    """

    def now(self) -> datetime:
        """Timezone-aware UTC. A naive datetime anywhere in this service is a bug."""
        ...


@runtime_checkable
class FeatureFlags(Protocol):
    """Boolean switches, read through one accessor.

    There is no ``os.environ.get("FLAG_...")`` outside ``app/flags.py``. ``merchant_id``
    exists for per-merchant rollouts; none of the four current flags use it, and the
    argument stays because removing it is a signature change across every caller.
    """

    def enabled(self, flag: str, *, merchant_id: str | None = None) -> bool:
        """True when the flag is on for this context."""
        ...


@runtime_checkable
class SessionFactory(Protocol):
    """Unit-of-work factory. **Every ``begin()`` is a new connection and a new transaction.**

    This is the binding rule the whole reconciliation design rests on
    (``interfaces.md`` §3.1): ``ReconciliationService.reconcile_batch`` calls ``begin()``
    three times, nested — a guard transaction that holds the batch advisory lock, a read
    transaction, and one short transaction per item. A reentrant or scoped implementation
    either deadlocks against itself or silently shares one transaction between the guard
    and the item work, at which point the advisory lock guards nothing.
    """

    def begin(self) -> AsyncContextManager[AsyncSession]:
        """Open a session on a fresh connection; commit on clean exit, roll back on raise."""
        ...


@runtime_checkable
class CircuitBreaker(Protocol):
    """One circuit per acquirer, in front of every outbound processor call.

    When the circuit is open ``call`` raises ``ProcessorUnavailableError`` **without
    making an HTTP request**. Without that, an acquirer degradation means four tasks x
    200 items per minute of capture attempts against something already returning 504s.
    """

    def is_open(self, name: str) -> bool:
        """True when the circuit is currently refusing calls. Cheap; no I/O on the hot path."""
        ...

    async def call(self, name: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn`` under circuit ``name``, recording the outcome in the window."""
        ...
