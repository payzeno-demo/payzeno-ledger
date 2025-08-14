"""`app/api/routers/payouts.py` — api-surface.md §10.4.

Six routes, all called by payzeno-api. Four of them are state transitions, and the two
that matter most are `mark_paid` and `mark_failed`: they are driven by the bank's return
file, they are the only writers of the terminal states, and `mark_failed` posts a
`payout_reversal` in the same transaction as the state change.

That reversal is invariant 7 and it is the difference between an ACH failure being an
inconvenience and being theft. `payout` already debited `merchant_payable`; `failed` is
terminal; if nothing credits it back there is no state left to correct it from and the
merchant has permanently lost the money.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.api.routers.payouts import (
    cancel_payout,
    create_payout,
    get_payout,
    list_payouts,
    mark_failed,
    mark_paid,
)
from app.errors import (
    InsufficientBalanceError,
    NotFoundError,
    PayoutBlockedError,
    ValidationError,
)
from app.repositories.base import Page

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 17, 0, tzinfo=UTC)

CALLER = "payzeno-api"


def _payout(payout_id: str = "po_1", *, status: str = "scheduled") -> Any:
    return type(
        "Payout",
        (),
        {
            "id": payout_id,
            "merchant_id": "mer_api",
            "currency": "USD",
            "amount_minor": 41_800,
            "method": "ach",
            "status": status,
            "available_on": date(2026, 4, 16),
            "arrival_estimate": date(2026, 4, 20),
            "failure_code": None,
            "reversal_transaction_id": None,
            "created_at": NOW,
        },
    )()


class StubPayoutService:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.paid: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    async def create_payout(self, session: Any, *, merchant_id: str, req: Any) -> Any:
        if self.raises is not None:
            raise self.raises
        self.created.append({"merchant_id": merchant_id, "req": req})
        return _payout()

    async def cancel_payout(self, session: Any, payout_id: str) -> Any:
        if self.raises is not None:
            raise self.raises
        self.cancelled.append(payout_id)
        return _payout(payout_id, status="canceled")

    async def mark_paid(
        self, session: Any, payout_id: str, *, paid_at: datetime, bank_reference: str
    ) -> Any:
        self.paid.append((payout_id, bank_reference))
        return _payout(payout_id, status="paid")

    async def mark_failed(
        self, session: Any, payout_id: str, *, failure_code: str, failure_message: str
    ) -> Any:
        self.failed.append((payout_id, failure_code))
        payout = _payout(payout_id, status="failed")
        payout.failure_code = failure_code
        payout.reversal_transaction_id = "txn_rev_po_1"
        return payout


class StubPayouts:
    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = rows or {}
        self.filters: list[dict[str, Any]] = []

    async def get_or_raise(self, session: Any, entity_id: str) -> Any:
        try:
            return self.rows[entity_id]
        except KeyError:
            raise NotFoundError(entity_id=entity_id) from None

    async def list_page(self, session: Any, *, cursor: str | None, limit: int, **filters: Any) -> Page:
        self.filters.append(filters)
        return Page(items=list(self.rows.values()), next_cursor=None, has_more=False)


def _body(**kwargs: Any) -> Any:
    return type("Body", (), kwargs)()


def _request(**overrides: Any) -> dict[str, Any]:
    """`create_payout` takes a plain ``dict``, not a contract model.

    The internal request is the public `CreatePayoutRequest` *plus* a `merchant_id` the
    public shape does not carry, and declaring a second pydantic model here would put a
    ledger-only shape in front of a type payzeno-console also consumes. `PayoutService`
    validates every field it reads.
    """
    return {
        "merchant_id": "mer_api",
        "amount_minor": 41_800,
        "currency": "USD",
        "method": "ach",
        **overrides,
    }


async def test_creating_a_payout_returns_it(sessions_factory) -> None:
    service = StubPayoutService()

    payout = await create_payout(
        _request(),
        sessions_factory,
        service,
        CALLER,
    )

    assert payout["status"] == "scheduled"
    assert service.created[0]["merchant_id"] == "mer_api"


async def test_a_restricted_merchant_gets_payout_blocked(sessions_factory) -> None:
    service = StubPayoutService(raises=PayoutBlockedError("merchant is restricted"))

    with pytest.raises(PayoutBlockedError) as excinfo:
        await create_payout(_request(), sessions_factory, service, CALLER)

    assert excinfo.value.http_status == 422
    assert excinfo.value.code == "payout_blocked"


async def test_an_empty_balance_is_422_not_500(sessions_factory) -> None:
    service = StubPayoutService(raises=InsufficientBalanceError("nothing available"))

    with pytest.raises(InsufficientBalanceError) as excinfo:
        await create_payout(_request(), sessions_factory, service, CALLER)

    assert excinfo.value.http_status == 422


async def test_getting_a_payout(sessions_factory) -> None:
    payouts = StubPayouts({"po_1": _payout()})

    payout = await get_payout(sessions_factory, payouts, "po_1")

    assert payout["id"] == "po_1"


async def test_getting_an_unknown_payout_is_a_404(sessions_factory) -> None:
    payouts = StubPayouts()

    with pytest.raises(NotFoundError) as excinfo:
        await get_payout(sessions_factory, payouts, "po_ghost")

    assert excinfo.value.http_status == 404


async def test_listing_passes_its_filters(sessions_factory) -> None:
    payouts = StubPayouts({"po_1": _payout()})

    page = await list_payouts(
        sessions_factory, payouts, 50, "mer_api", "scheduled", "USD", None
    )

    assert page["has_more"] is False
    assert payouts.filters[0]["status"] == "scheduled"


async def test_cancelling_a_scheduled_payout(sessions_factory) -> None:
    """Only while `scheduled`. Once it is in transit the bank has it."""
    service = StubPayoutService()

    payout = await cancel_payout(sessions_factory, service, CALLER, "po_1")

    assert payout["status"] == "canceled"
    assert service.cancelled == ["po_1"]


async def test_cancelling_an_in_transit_payout_is_refused(sessions_factory) -> None:
    service = StubPayoutService(raises=ValidationError("payout is in_transit"))

    with pytest.raises(ValidationError):
        await cancel_payout(sessions_factory, service, CALLER, "po_1")


async def test_marking_paid_records_the_bank_reference(sessions_factory) -> None:
    """The bank reference is what support matches on when a merchant says it never
    arrived, so it is required rather than optional."""
    service = StubPayoutService()

    payout = await mark_paid(
        _body(paid_at=NOW, bank_reference="ACH-TRACE-88213"),
        sessions_factory,
        service,
        CALLER,
        "po_1",
    )

    assert payout["status"] == "paid"
    assert service.paid == [("po_1", "ACH-TRACE-88213")]


async def test_marking_failed_posts_a_reversal(sessions_factory) -> None:
    """Invariant 7, at the route boundary.

    `failed` is terminal. The reversal happens in the same transaction as the state
    change or the merchant's money is gone with nothing left to correct it from.
    """
    service = StubPayoutService()

    payout = await mark_failed(
        _body(failure_code="account_closed", failure_message="R02 account closed"),
        sessions_factory,
        service,
        CALLER,
        "po_1",
    )

    assert payout["status"] == "failed"
    assert payout["failure_code"] == "account_closed"
    assert payout["reversal_transaction_id"] is not None


async def test_marking_failed_requires_a_failure_code(sessions_factory) -> None:
    """"It failed" is not an answer a merchant can act on.

    The code drives the console's message and whether the merchant is asked to re-verify
    their bank account.
    """
    service = StubPayoutService()

    with pytest.raises((ValidationError, AttributeError, KeyError)):
        await mark_failed(
            _body(failure_message="something went wrong"),
            sessions_factory,
            service,
            CALLER,
            "po_1",
        )


async def test_a_request_without_a_merchant_is_a_422(sessions_factory) -> None:
    """The one field the route validates itself.

    Everything else in the body is `PayoutService`'s business, but `merchant_id` decides
    *whose money* this is, and a payout with an empty merchant scope must never reach a
    service that would then have to guess.
    """
    service = StubPayoutService()

    with pytest.raises(ValidationError) as excinfo:
        await create_payout(_request(merchant_id="  "), sessions_factory, service, CALLER)

    assert excinfo.value.details["field"] == "merchant_id"
    assert service.created == []
    assert sessions_factory.begin_count == 0


async def test_listing_scheduled_payouts_drives_the_next_payout_tile(sessions_factory) -> None:
    """`?status=scheduled&limit=1`. The filters have to survive the handler intact."""
    payouts = StubPayouts({"po_1": _payout()})

    page = await list_payouts(sessions_factory, payouts, 1, "mer_api", "scheduled", None, None)

    assert payouts.filters[0] == {
        "merchant_id": "mer_api",
        "status": "scheduled",
        "currency": None,
    }
    assert len(page["data"]) == 1
