"""`PayoutInitiator` and its four concretes — app/services/rails/.

One abstract base, four rails, four calendars, four cutoffs. There is deliberately no
single `PAYOUT_CUTOFF_HOUR_UTC`: SEPA, Faster Payments and the two ACH rails are in
different jurisdictions and share neither holidays nor cutoffs, which is exactly the
mistake this suite is here to keep out.

Three of the four `initiate` bodies still return a synthetic `rail_reference` and carry
`# TODO(mhandover): wire the real SFTP drop once treasury signs off`. Michal left before
treasury did. The tests assert the shape of what they return rather than pretending the
submission is real, because a test that asserts a stub is a stub is at least honest about
what is not finished.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.errors import BankAccountUnusableError
from app.services.payouts import PayoutInitiator
from app.services.rails.ach import AchPayoutInitiator, SameDayAchPayoutInitiator
from app.services.rails.debit_ach import AchPayoutPuller
from app.services.rails.faster_payments import FasterPaymentsPayoutInitiator
from app.services.rails.sepa import SepaPayoutInitiator
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)


class StubCalendar:
    def __init__(self) -> None:
        self.calls: list[tuple[date, str, str]] = []

    def next_business_day(self, day: date, currency: str, rail: str) -> date:
        self.calls.append((day, currency, rail))
        # Friday the 17th for everything except the weekend cases below.
        while day.weekday() >= 5:
            day = date.fromordinal(day.toordinal() + 1)
        return day

    def is_business_day(self, day: date, currency: str, rail: str) -> bool:
        return day.weekday() < 5


class StubSettings:
    payout_cutoff_ach_utc = "21:00"
    payout_cutoff_same_day_ach_utc = "16:45"
    payout_cutoff_sepa_utc = "14:00"
    payout_cutoff_faster_payments_utc = "17:30"


def _payout(*, method: str = "ach", currency: str = "USD") -> Any:
    return type(
        "Payout",
        (),
        {
            "id": "po_rail_1",
            "merchant_id": "mer_rail",
            "amount_minor": 125_000,
            "currency": currency,
            "method": method,
            "status": "scheduled",
            "available_on": date(2026, 4, 16),
            "statement_descriptor": "PAYZENO PAYOUT",
            "bank_account_id": "ba_rail_1",
        },
    )()


def _bank(*, status: str = "verified", country: str = "US", currency: str = "USD") -> Any:
    return type(
        "BankAccountProjection",
        (),
        {
            "id": "ba_rail_1",
            "merchant_id": "mer_rail",
            "currency": currency,
            "country": country,
            "status": status,
            "account_number_token": "tok_rail_1",
            "account_last_four": "4242",
            "routing_number_last_four": "0021",
            "is_default": True,
        },
    )()


ALL_INITIATORS = [
    (AchPayoutInitiator, "ach", "USD", "US"),
    (SameDayAchPayoutInitiator, "same_day_ach", "USD", "US"),
    (SepaPayoutInitiator, "sepa", "EUR", "DE"),
    (FasterPaymentsPayoutInitiator, "faster_payments", "GBP", "GB"),
]


def _initiator(cls: type) -> Any:
    return cls(StubSettings(), StubCalendar(), FrozenClock(NOW))


# --------------------------------------------------------------------------------------
# the base
# --------------------------------------------------------------------------------------


async def test_payout_initiator_is_abstract() -> None:
    with pytest.raises(TypeError):
        PayoutInitiator()  # type: ignore[abstract]


@pytest.mark.parametrize(("cls", "method", "_currency", "_country"), ALL_INITIATORS)
async def test_every_rail_declares_its_method(
    cls: type, method: str, _currency: str, _country: str
) -> None:
    """`method` is the key `PayoutService.initiators` is looked up by.

    A rail whose `method` does not match `payout.method` is unreachable and the payout
    sits `scheduled` forever with nobody noticing.
    """
    assert issubclass(cls, PayoutInitiator)
    assert cls.method == method


@pytest.mark.parametrize(("cls", "method", "currency", "country"), ALL_INITIATORS)
async def test_every_rail_returns_a_complete_initiation_result(
    cls: type, method: str, currency: str, country: str
) -> None:
    initiator = _initiator(cls)

    result = await initiator.initiate(
        object(),
        _payout(method=method, currency=currency),
        _bank(currency=currency, country=country),
    )

    assert result.rail_reference
    assert isinstance(result.arrival_estimate, date)
    assert result.submitted_at == NOW


@pytest.mark.parametrize(("cls", "method", "currency", "country"), ALL_INITIATORS)
async def test_every_rail_refuses_an_unverified_bank_account(
    cls: type, method: str, currency: str, country: str
) -> None:
    """`_assert_usable` on the base. Four rails, one guard, no way to skip it."""
    initiator = _initiator(cls)

    with pytest.raises(BankAccountUnusableError):
        await initiator.initiate(
            object(),
            _payout(method=method, currency=currency),
            _bank(status="pending", currency=currency, country=country),
        )


@pytest.mark.parametrize(("cls", "method", "currency", "country"), ALL_INITIATORS)
async def test_every_rail_asks_the_calendar_for_its_own_arrival_date(
    cls: type, method: str, currency: str, country: str
) -> None:
    """Four jurisdictions, four calendars. The rail name has to reach the calendar."""
    calendar = StubCalendar()
    initiator = cls(StubSettings(), calendar, FrozenClock(NOW))

    await initiator.initiate(
        object(),
        _payout(method=method, currency=currency),
        _bank(currency=currency, country=country),
    )

    assert calendar.calls, "the rail computed an arrival date without the calendar"
    assert calendar.calls[0][2] == method
    assert calendar.calls[0][1] == currency


# --------------------------------------------------------------------------------------
# per-rail specifics
# --------------------------------------------------------------------------------------


async def test_same_day_ach_extends_standard_ach() -> None:
    """Real inheritance, not a copy.

    Same-day ACH is standard ACH with a different cutoff and a same-day arrival. It
    subclasses rather than duplicating `_build_reference`, which is what keeps the two
    references in the same format for treasury's reconciliation spreadsheet.
    """
    assert issubclass(SameDayAchPayoutInitiator, AchPayoutInitiator)


async def test_same_day_ach_arrives_before_standard_ach() -> None:
    standard = _initiator(AchPayoutInitiator)
    same_day = _initiator(SameDayAchPayoutInitiator)

    slow = await standard.initiate(object(), _payout(method="ach"), _bank())
    fast = await same_day.initiate(
        object(), _payout(method="same_day_ach"), _bank()
    )

    assert fast.arrival_estimate <= slow.arrival_estimate


async def test_ach_reference_carries_the_payout_id() -> None:
    """Treasury greps for the payout id when a bank asks about a trace number."""
    initiator = _initiator(AchPayoutInitiator)

    result = await initiator.initiate(object(), _payout(), _bank())

    assert "po_rail_1" in result.rail_reference


async def test_sepa_refuses_a_non_euro_payout() -> None:
    initiator = _initiator(SepaPayoutInitiator)

    with pytest.raises(BankAccountUnusableError):
        await initiator.initiate(
            object(),
            _payout(method="sepa", currency="EUR"),
            _bank(currency="USD", country="US"),
        )


async def test_faster_payments_refuses_a_non_gbp_payout() -> None:
    initiator = _initiator(FasterPaymentsPayoutInitiator)

    with pytest.raises(BankAccountUnusableError):
        await initiator.initiate(
            object(),
            _payout(method="faster_payments", currency="GBP"),
            _bank(currency="EUR", country="DE"),
        )


# --------------------------------------------------------------------------------------
# the debit puller — the one rail that moves money the other way
# --------------------------------------------------------------------------------------


class StubBanks:
    def __init__(self, bank: Any) -> None:
        self.bank = bank

    async def get_default(self, session: Any, *, merchant_id: str, currency: str) -> Any:
        return self.bank


class StubLedger:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, session: Any, **kwargs: Any) -> Any:
        self.posts.append(kwargs)
        transaction = type("Txn", (), {"id": "txn_pull_1"})()
        return type("PostResult", (), {"transaction": transaction, "created": True})()


async def test_debit_pull_posts_and_returns_a_rail_reference() -> None:
    """Negative balance recovery. Rare, and the one place we debit a merchant's bank."""
    ledger = StubLedger()
    puller = AchPayoutPuller(
        banks=StubBanks(_bank()),
        ledger=ledger,
        calendar=StubCalendar(),
        settings=StubSettings(),
        clock=FrozenClock(NOW),
    )

    result = await puller.pull(
        object(),
        merchant_id="mer_rail",
        currency="USD",
        amount_minor=25_000,
        reason="negative_balance_recovery",
    )

    assert result.amount_minor == 25_000
    assert result.rail_reference
    assert ledger.posts


async def test_debit_pull_refuses_an_unusable_account() -> None:
    puller = AchPayoutPuller(
        banks=StubBanks(_bank(status="errored")),
        ledger=StubLedger(),
        calendar=StubCalendar(),
        settings=StubSettings(),
        clock=FrozenClock(NOW),
    )

    with pytest.raises(BankAccountUnusableError):
        await puller.pull(
            object(),
            merchant_id="mer_rail",
            currency="USD",
            amount_minor=25_000,
            reason="negative_balance_recovery",
        )
