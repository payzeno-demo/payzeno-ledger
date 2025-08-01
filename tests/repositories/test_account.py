"""`AccountRepository` — app/repositories/account.py.

The interesting part of this table is not the repository, it is the two unique indexes:
`uq_account_merchant_type_currency_livemode` for merchant accounts and the partial
`pix_account_platform` for the ones with a NULL merchant_id. A NULL does not participate in
a normal unique constraint, so without the partial variant we would happily create the
platform `cash` account four times.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.errors import AccountNotFoundError
from app.models.account import Account
from app.repositories.account import AccountRepository
from tests.factories import make_account_set

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def repo() -> AccountRepository:
    return AccountRepository()


async def test_add_and_fetch(session, repo: AccountRepository) -> None:
    account = make_account_set(merchant_id="mer_acc_1", currency="USD", livemode=True)[0]
    await repo.add(session, account)
    await session.flush()

    assert (await repo.get_or_raise(session, account.id)).type == account.type


async def test_find_one_by_natural_key(session, repo: AccountRepository) -> None:
    for account in make_account_set(merchant_id="mer_acc_2", currency="USD", livemode=True):
        await repo.add(session, account)
    await session.flush()

    found = await repo.find_one(
        session,
        merchant_id="mer_acc_2",
        type_="merchant_payable",
        currency="USD",
        livemode=True,
    )

    assert found is not None
    assert found.normal_balance == "credit"


async def test_natural_key_is_unique_per_livemode(session, repo: AccountRepository) -> None:
    """Test-mode money must never touch a live account.

    Same merchant, same type, same currency, different livemode -> two rows, and that is
    correct. `livemode` is part of the unique key precisely so it is representable.
    """
    live = make_account_set(merchant_id="mer_acc_3", currency="USD", livemode=True)[0]
    test = make_account_set(merchant_id="mer_acc_3", currency="USD", livemode=False)[0]

    await repo.add(session, live)
    await repo.add(session, test)
    await session.flush()

    assert live.id != test.id


async def test_duplicate_natural_key_violates_the_unique_index(
    session, repo: AccountRepository
) -> None:
    first = make_account_set(merchant_id="mer_acc_4", currency="USD", livemode=True)[0]
    duplicate = make_account_set(merchant_id="mer_acc_4", currency="USD", livemode=True)[0]
    duplicate.id = "acct_a_different_id"

    await repo.add(session, first)
    await session.flush()
    await repo.add(session, duplicate)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_platform_accounts_have_a_null_merchant_and_their_own_unique_index(
    session, repo: AccountRepository
) -> None:
    platform_cash = Account(
        id="acct_platform_cash_usd",
        merchant_id=None,
        type="cash",
        currency="USD",
        normal_balance="debit",
        status="active",
        livemode=True,
    )
    await repo.add(session, platform_cash)
    await session.flush()

    clash = Account(
        id="acct_platform_cash_usd_2",
        merchant_id=None,
        type="cash",
        currency="USD",
        normal_balance="debit",
        status="active",
        livemode=True,
    )
    await repo.add(session, clash)

    # This is the one a plain unique constraint would let through, because NULL != NULL.
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_list_for_merchant_filters_by_currency(session, repo: AccountRepository) -> None:
    for account in make_account_set(merchant_id="mer_acc_5", currency="USD", livemode=True):
        await repo.add(session, account)
    for account in make_account_set(merchant_id="mer_acc_5", currency="EUR", livemode=True):
        await repo.add(session, account)
    await session.flush()

    usd = await repo.list_for_merchant(session, merchant_id="mer_acc_5", currency="USD")

    assert usd
    assert {a.currency for a in usd} == {"USD"}


async def test_freeze_flips_the_status_and_nothing_else(session, repo: AccountRepository) -> None:
    account = make_account_set(merchant_id="mer_acc_6", currency="USD", livemode=True)[0]
    await repo.add(session, account)
    await session.flush()

    frozen = await repo.set_status(session, account.id, status="frozen")

    assert frozen.status == "frozen"
    assert frozen.normal_balance == account.normal_balance
    assert frozen.currency == account.currency


async def test_set_status_on_a_missing_account_raises(session, repo: AccountRepository) -> None:
    with pytest.raises(AccountNotFoundError):
        await repo.set_status(session, "acct_nope", status="frozen")
