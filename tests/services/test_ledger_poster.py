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

        direction=direction,  # type: ignore[arg-type]
        transactions=transactions,
        entries=entries,
        resolver=StubResolver(frozen=frozen),
        publisher=CollectingPublisher(),
        clock=FrozenClock(),
    )
    return poster, transactions, entries, cache


def post_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "idempotency_key": "settle:sb_A:ri_1",
        "purpose": "settle",
        "merchant_id": "mer_1",
        "currency": "USD",
        "livemode": True,
        "reference_type": "reconciliation_item",
        "reference_id": "ri_1",
        "lines": BALANCED,
        "created_by": "reconciliation",
        "request_fingerprint": "a" * 64,
    }
    base.update(overrides)
    return base


async def test_post_writes_one_transaction_and_its_entries() -> None:
    poster, transactions, entries, _ = build()

    result = await poster.post(NOW_SESSION, **post_kwargs())

    assert isinstance(result, PostResult)
    assert result.created is True
    assert len(transactions.by_key) == 1
    assert len(entries.rows) == 2


async def test_entries_are_sequenced_from_zero() -> None:
    poster, _, entries, _ = build()

    await poster.post(NOW_SESSION, **post_kwargs())

    assert [e.sequence for e in entries.rows] == [0, 1]


async def test_invariant_1_unbalanced_lines_are_rejected() -> None:
    poster, _, entries, _ = build()

    with pytest.raises(UnbalancedTransactionError):
        await poster.post(
            NOW_SESSION,
            **post_kwargs(
                lines=[
                    line("processor_clearing", "debit", 10_000),
                    line("merchant_payable", "credit", 9_999),
                ]
            ),
        )
    assert entries.rows == []


async def test_invariant_2_a_single_leg_is_rejected() -> None:
    poster, _, _, _ = build()

    with pytest.raises(UnbalancedTransactionError):
        await poster.post(NOW_SESSION, **post_kwargs(lines=[line("cash", "debit", 1)]))


async def test_invariant_4_a_negative_amount_is_rejected() -> None:
    poster, _, _, _ = build()

    with pytest.raises(NegativeAmountError):
        await poster.post(
            NOW_SESSION,
            **post_kwargs(
                lines=[
                    line("processor_clearing", "debit", -10_000),
                    line("merchant_payable", "credit", -10_000),
                ]
            ),
        )


async def test_invariant_3_currency_mismatch_is_rejected() -> None:
    # The resolver hands back USD accounts. Posting a EUR transaction through them puts a
    # EUR entry on a USD account, which is the mismatch invariant 3 is about.
    poster, _, _, _ = build()

    with pytest.raises(CurrencyMismatchError):
        await poster.post(NOW_SESSION, **post_kwargs(currency="EUR"))


async def test_invariant_5_livemode_mismatch_is_rejected() -> None:
    """Test-mode money must never touch a live account.

    The accounts resolver hands back accounts stamped livemode=True; posting a livemode=False
    transaction through them is the mismatch. There is no plausible business case and a very
    plausible way to end up with test volume in a real trial balance.
    """
    poster, _, _, _ = build()

    with pytest.raises(LivemodeMismatchError):
        await poster.post(NOW_SESSION, **post_kwargs(livemode=False))


async def test_a_frozen_account_raises_the_409() -> None:
    poster, _, entries, _ = build(frozen={"merchant_payable"})

    with pytest.raises(AccountFrozenError) as excinfo:
        await poster.post(NOW_SESSION, **post_kwargs())

    assert excinfo.value.http_status == 409
    assert entries.rows == []


async def test_on_conflict_raise_is_the_default() -> None:
    poster, _, _, _ = build()

    await poster.post(NOW_SESSION, **post_kwargs())

    with pytest.raises(DuplicateSettlementError):
        await poster.post(NOW_SESSION, **post_kwargs())


async def test_on_conflict_return_existing_gives_created_false() -> None:
    poster, _, entries, _ = build()

    first = await poster.post(NOW_SESSION, **post_kwargs(on_conflict="return_existing"))
    second = await poster.post(NOW_SESSION, **post_kwargs(on_conflict="return_existing"))

    assert first.created is True
    assert second.created is False
    assert second.transaction.id == first.transaction.id
    # entries are written only when created is true — six entries for one settlement is the
    # observable end state of PAY-2041 and it must not be reachable through this path.
    assert len(entries.rows) == 2


async def test_the_balance_cache_moves_in_the_same_transaction() -> None:
    # data-model.md §3.10. The cache can never lag a committed posting, which is exactly why
    # `LedgerBalanceCacheDrift` firing at 01:26 was a real signal about something else.
    poster, _, _, cache = build()

    await poster.post(NOW_SESSION, **post_kwargs())

    assert cache.deltas
    assert cache.deltas[0]["merchant_id"] == "mer_1"
    assert cache.deltas[0]["currency"] == "USD"


async def test_accounts_are_resolved_lazily_through_the_one_writer() -> None:
    # There is one writer of `account` — AccountResolver.get_or_create — with three callers:
    # the bootstrap route, MerchantEventConsumer's merchant.created branch, and this, the
    # lazy resolution at posting time. Not three competing mechanisms.
    resolver = StubResolver()
    poster = LedgerPoster(
        entries=InMemoryEntries(),
        accounts=InMemoryAccounts(),
        balances=InMemoryBalanceCache(),
        resolver=resolver,
        publisher=CollectingPublisher(),
        clock=FrozenClock(),
    )

    await poster.post(NOW_SESSION, **post_kwargs())

    assert resolver.resolved == ["processor_clearing", "merchant_payable"]


async def test_publishes_transaction_posted() -> None:
    transactions = InMemoryTransactions()
    publisher = CollectingPublisher()
    poster = LedgerPoster(
        entries=InMemoryEntries(),
        accounts=InMemoryAccounts(),
        resolver=StubResolver(),
        clock=FrozenClock(),
    )

    await poster.post(NOW_SESSION, **post_kwargs())

    assert publisher.event_types() == ["ledger.transaction_posted"]
