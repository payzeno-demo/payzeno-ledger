"""`BaseRepository` — app/repositories/base.py.

I keep having to re-explain this one in review, so it is written down here instead.

The repositories are STATELESS. They are constructed once in app/container.py and shared by
everything: the sweep's guard session, the sweep's read session, the sweep's per-item
session and the retry drain's own session all call methods on the *same* instance. That is
why every method takes `session` as its first positional argument and why there is no
`self._session` anywhere in the package.

If you are wondering whether it would be tidier to bind a session in `__init__` — it would,
and it would also make `ReconciliationService.reconcile_batch` impossible to write, because
that method holds three sessions at once on purpose. See interfaces.md §3.2.
"""

from __future__ import annotations

import pytest

from app.errors import AccountNotFoundError, NotFoundError
from app.models.account import Account
from app.repositories.account import AccountRepository
from app.repositories.base import BaseRepository, Page
from tests.factories import make_account_set

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_base_repository_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseRepository()  # type: ignore[abstract]


async def test_a_concrete_repository_declares_its_model() -> None:
    assert AccountRepository.model is Account
    assert issubclass(AccountRepository, BaseRepository)


async def test_repository_instances_hold_no_session(session) -> None:
    """The property the whole reconciliation subsystem leans on.

    Two questions I asked in review and the answers I got:
      - "why not bind it?"        because one instance serves four concurrent sessions
      - "why not a thread-local?" because these are coroutines on one loop, not threads
    """
    repo = AccountRepository()

    assert not hasattr(repo, "_session")
    assert "session" not in vars(repo)
    # constructing it twice must give two interchangeable, empty objects
    assert vars(repo) == vars(AccountRepository())


async def test_get_returns_none_for_a_missing_id(session) -> None:
    repo = AccountRepository()
    assert await repo.get(session, "acct_does_not_exist") is None


async def test_get_or_raise_raises_the_aggregate_specific_error(session) -> None:
    repo = AccountRepository()

    with pytest.raises(AccountNotFoundError) as excinfo:
        await repo.get_or_raise(session, "acct_does_not_exist")

    # Every aggregate has its own subclass so a 404 body says WHICH thing was missing.
    assert isinstance(excinfo.value, NotFoundError)
    assert excinfo.value.details["entity_id"] == "acct_does_not_exist"


async def test_add_then_get_round_trips(session) -> None:
    repo = AccountRepository()
    accounts = make_account_set(merchant_id="mer_base_1", currency="USD", livemode=True)

    added = await repo.add(session, accounts[0])
    await session.flush()

    fetched = await repo.get(session, added.id)
    assert fetched is not None
    assert fetched.id == added.id
    assert fetched.merchant_id == "mer_base_1"


async def test_the_same_instance_serves_two_sessions(sessions) -> None:
    """This is the test that actually justifies the design.

    One repository, two independent transactions on two connections, no interference. If a
    session were bound to the instance the second `begin()` would either reuse the first
    one's transaction or blow up.
    """
    repo = AccountRepository()
    accounts = make_account_set(merchant_id="mer_base_2", currency="EUR", livemode=True)

    async with sessions.begin() as first:
        await repo.add(first, accounts[0])

    async with sessions.begin() as second:
        found = await repo.get(second, accounts[0].id)
        assert found is not None


async def test_list_page_returns_a_page_and_a_cursor(session) -> None:
    repo = AccountRepository()
    for account in make_account_set(merchant_id="mer_base_3", currency="USD", livemode=True):
        await repo.add(session, account)
    await session.flush()

    page = await repo.list_page(session, cursor=None, limit=2, merchant_id="mer_base_3")

    assert isinstance(page, Page)
    assert len(page.items) == 2
    assert page.has_more is True
    assert page.next_cursor is not None


async def test_list_page_walks_to_the_end_without_repeating_a_row(session) -> None:
    repo = AccountRepository()
    for account in make_account_set(merchant_id="mer_base_4", currency="USD", livemode=True):
        await repo.add(session, account)
    await session.flush()

    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await repo.list_page(session, cursor=cursor, limit=2, merchant_id="mer_base_4")
        seen.extend(item.id for item in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor

    assert len(seen) == len(set(seen))


async def test_list_page_of_an_empty_result_is_a_terminal_page(session) -> None:
    repo = AccountRepository()
    page = await repo.list_page(session, cursor=None, limit=10, merchant_id="mer_nobody")

    assert page.items == []
    assert page.has_more is False
    assert page.next_cursor is None


async def test_default_order_is_declared_by_every_concrete_repository() -> None:
    # It is abstract on the base for a reason: keyset pagination without a deterministic
    # order silently drops and duplicates rows across pages.
    repo = AccountRepository()
    assert repo._default_order() is not None
