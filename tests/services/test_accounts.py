"""`AccountResolver` — app/services/accounts.py.

The single writer of the `account` table. Three entry points delegate to it — the bootstrap
route, `MerchantEventConsumer`'s `merchant.created` branch, and lazy resolution inside
`LedgerPoster.post` — and they are one writer with three callers, not three mechanisms with
an undefined precedence.

Idempotency comes from `uq_account_merchant_type_currency_livemode`, not from a check in
Python. `get_or_create` inserts with `ON CONFLICT DO NOTHING` and re-reads on conflict, so
two concurrent `merchant.created` deliveries produce one row and no error. Reading first and
inserting second is the shape ADR 0011 tells us not to write.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models.account import Account
from app.repositories.account import AccountRepository
from app.services.accounts import AccountResolver
from tests.doubles import FrozenClock

pytestmark = pytest.mark.asyncio

SESSION = object()

# Every account a merchant needs before any money can move, per domain-model.md §6.
EXPECTED_BOOTSTRAP_TYPES = {
    "merchant_receivable",
    "authorization_hold",
    "merchant_payable",
    "reserve",
    "chargeback_liability",
}


class InMemoryAccounts(AccountRepository):
    """The unique index, in a dictionary."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[tuple[str | None, str, str, bool], Account] = {}
        self.insert_attempts = 0
        self._seq = 0

    async def insert_on_conflict_nothing(self, session: Any, account: Account) -> Account | None:
        self.insert_attempts += 1
        key = (account.merchant_id, account.type, account.currency, account.livemode)
        if key in self.rows:
            return None
        self.rows[key] = account
        return account

    async def find_one(
        self, session: Any, *, merchant_id: str | None, type_: str, currency: str, livemode: bool
    ) -> Account | None:
        return self.rows.get((merchant_id, type_, currency, livemode))

    def next_id(self) -> str:
        self._seq += 1
        return f"acct_{self._seq:04d}"


@pytest.fixture
def accounts() -> InMemoryAccounts:
    return InMemoryAccounts()


@pytest.fixture
def resolver(accounts: InMemoryAccounts) -> AccountResolver:
    return AccountResolver(accounts=accounts, clock=FrozenClock())


async def test_get_or_create_creates_on_first_call(resolver, accounts) -> None:
    account = await resolver.get_or_create(
        SESSION, merchant_id="mer_1", type_="merchant_payable", currency="USD", livemode=True
    )

    assert account.type == "merchant_payable"
    assert account.merchant_id == "mer_1"
    assert len(accounts.rows) == 1


async def test_get_or_create_is_idempotent(resolver, accounts) -> None:
    first = await resolver.get_or_create(
        SESSION, merchant_id="mer_2", type_="reserve", currency="USD", livemode=True
    )
    second = await resolver.get_or_create(
        SESSION, merchant_id="mer_2", type_="reserve", currency="USD", livemode=True
    )

    assert first.id == second.id
    assert len(accounts.rows) == 1


async def test_a_conflict_re_reads_rather_than_raising(resolver, accounts) -> None:
    """Two concurrent merchant.created deliveries must produce one row and no error.

    The insert is attempted every time — that is deliberate. The database decides, and the
    loser reads the winner's row back. There is no `if exists` in front of it.
    """
    await resolver.get_or_create(
        SESSION, merchant_id="mer_3", type_="merchant_payable", currency="USD", livemode=True
    )
    await resolver.get_or_create(
        SESSION, merchant_id="mer_3", type_="merchant_payable", currency="USD", livemode=True
    )

    assert accounts.insert_attempts == 2
    assert len(accounts.rows) == 1


async def test_normal_balance_is_derived_from_the_type(resolver) -> None:
    payable = await resolver.get_or_create(
        SESSION, merchant_id="mer_4", type_="merchant_payable", currency="USD", livemode=True
    )
    clearing = await resolver.get_or_create(
        SESSION, merchant_id="mer_4", type_="processor_clearing", currency="USD", livemode=True
    )

    assert payable.normal_balance == "credit"
    assert clearing.normal_balance == "debit"


async def test_livemode_and_currency_are_part_of_the_identity(resolver, accounts) -> None:
    await resolver.get_or_create(
        SESSION, merchant_id="mer_5", type_="merchant_payable", currency="USD", livemode=True
    )
    await resolver.get_or_create(
        SESSION, merchant_id="mer_5", type_="merchant_payable", currency="USD", livemode=False
    )
    await resolver.get_or_create(
        SESSION, merchant_id="mer_5", type_="merchant_payable", currency="EUR", livemode=True
    )

    assert len(accounts.rows) == 3


async def test_platform_accounts_carry_a_null_merchant(resolver, accounts) -> None:
    platform = await resolver.get_or_create(
        SESSION, merchant_id=None, type_="cash", currency="USD", livemode=True
    )

    assert platform.merchant_id is None
    assert (None, "cash", "USD", True) in accounts.rows


async def test_bootstrap_creates_the_full_merchant_set(resolver, accounts) -> None:
    created = await resolver.bootstrap(
        SESSION, merchant_id="mer_6", currency="USD", livemode=True
    )

    assert {a.type for a in created} >= EXPECTED_BOOTSTRAP_TYPES


async def test_bootstrap_is_idempotent(resolver, accounts) -> None:
    # POST /internal/v1/accounts/bootstrap is called synchronously by POST /v1/merchants AND
    # asynchronously by the merchant.created consumer. Both will happen. Calling it twice is
    # a no-op by construction.
    first = await resolver.bootstrap(SESSION, merchant_id="mer_7", currency="USD", livemode=True)
    row_count = len(accounts.rows)
    second = await resolver.bootstrap(SESSION, merchant_id="mer_7", currency="USD", livemode=True)

    assert len(accounts.rows) == row_count
    assert {a.id for a in first} == {a.id for a in second}


async def test_bootstrap_for_a_second_currency_adds_a_second_set(resolver, accounts) -> None:
    await resolver.bootstrap(SESSION, merchant_id="mer_8", currency="USD", livemode=True)
    before = len(accounts.rows)
    await resolver.bootstrap(SESSION, merchant_id="mer_8", currency="GBP", livemode=True)

    assert len(accounts.rows) == before * 2


async def test_an_unknown_account_type_is_rejected(resolver) -> None:
    with pytest.raises(ValueError, match="account type"):
        await resolver.get_or_create(
            SESSION, merchant_id="mer_9", type_="petty_cash", currency="USD", livemode=True
        )
