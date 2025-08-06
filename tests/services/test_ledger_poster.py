"""`LedgerPoster` — app/services/transactions.py.

The one writer of `ledger_transaction` + `ledger_entry`. Nothing else INSERTs them, which is
why the `ON CONFLICT` added by PR #172 had to live here and not in the caller.

Invariants 1-5 from domain-model.md §7 are enforced before any row is written. Each one has
its own exception and its own test below, because "it raised something" is not the contract
— `app/api/error_handlers.py` maps the specific class onto the specific status, and
payzeno-api's `LedgerHttpClient` branches on it.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.postings import PostingLine
from app.errors import (
    AccountFrozenError,
    CurrencyMismatchError,
    DuplicateSettlementError,
    LivemodeMismatchError,
    NegativeAmountError,
    UnbalancedTransactionError,
)
from app.repositories.account import AccountRepository
from app.repositories.balance_cache import MerchantBalanceCacheRepository
from app.repositories.ledger_entry import LedgerEntryRepository
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.services.accounts import AccountResolver
from app.services.transactions import LedgerPoster, PostResult
from tests.doubles import CollectingPublisher, FrozenClock

pytestmark = pytest.mark.asyncio

NOW_SESSION = object()


class FakeAccount:
    def __init__(self, account_id: str, type_: str, status: str = "active") -> None:
        self.id = account_id
        self.type = type_
        self.status = status
        self.currency = "USD"
        self.livemode = True


class InMemoryAccounts(AccountRepository):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, FakeAccount] = {}

    async def find_one(self, session: Any, **key: Any) -> FakeAccount | None:
        return self.rows.get(str(key.get("type_")))


class StubResolver(AccountResolver):
    """Lazy account resolution, with a frozen account available on demand."""

    def __init__(self, frozen: set[str] | None = None) -> None:
        self.frozen = frozen or set()
        self.resolved: list[str] = []

    async def get_or_create(
        self, session: Any, *, merchant_id: str | None, type_: str, currency: str, livemode: bool
    ) -> FakeAccount:
        self.resolved.append(type_)
        status = "frozen" if type_ in self.frozen else "active"
        return FakeAccount(f"acct_{type_}", type_, status=status)


class InMemoryTransactions(LedgerTransactionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.by_key: dict[str, Any] = {}
        self._seq = 0

    async def claim_idempotency_key(self, session: Any, **kw: Any) -> Any:
        from app.services.transactions import IdempotencyClaim

        entries=entries,
        resolver=StubResolver(frozen=frozen),
        balances=InMemoryBalanceCache(),
        clock=FrozenClock(),
    )

    await poster.post(NOW_SESSION, **post_kwargs())

    assert resolver.resolved == ["processor_clearing", "merchant_payable"]


async def test_publishes_transaction_posted() -> None:
    transactions = InMemoryTransactions()
    publisher = CollectingPublisher()
    poster = LedgerPoster(
        accounts=InMemoryAccounts(),
    )

    await poster.post(NOW_SESSION, **post_kwargs())

    assert publisher.event_types() == ["ledger.transaction_posted"]
