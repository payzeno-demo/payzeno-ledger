"""Account resolution — the single writer of the ``account`` table.

Three callers, one writer:

* ``POST /internal/v1/accounts/bootstrap`` (payzeno-api, at merchant onboarding)
* ``MerchantEventConsumer``'s ``merchant.created`` branch
* lazy resolution at posting time, from ``LedgerPoster._materialise_entries``

There are not three mechanisms; there is one idempotent ``get_or_create`` with three
callers. ``uq_account_merchant_type_currency_livemode`` is what makes it idempotent —
a concurrent create loses the INSERT and re-reads.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ids import new_id
from app.errors import AccountFrozenError, AccountNotFoundError, ValidationError
from app.logging import get_logger
from app.models.account import Account
from app.ports import Clock
from app.repositories.account import AccountRepository

logger = get_logger(__name__)

#: The account set every merchant gets on bootstrap, per currency and livemode.
MERCHANT_ACCOUNT_TYPES: tuple[str, ...] = (
    "merchant_payable",
    "merchant_pending",
    "merchant_reserve",
    "merchant_disputed",
)

#: Normal balance per account type. `domain-model.md` §6 — a liability account's normal
#: balance is a credit, an asset account's is a debit.
NORMAL_BALANCE: dict[str, str] = {
    "merchant_payable": "credit",
    "merchant_pending": "credit",
    "merchant_reserve": "credit",
    "merchant_disputed": "credit",
    "platform_fee_revenue": "credit",
    "platform_acquirer_expense": "debit",
    "acquirer_receivable": "debit",
    "cash": "debit",
}


class AccountResolver:
    """Resolves — and creates on first use — the ledger accounts a posting needs."""

    def __init__(self, accounts: AccountRepository, clock: Clock) -> None:
        self._accounts = accounts
        self._clock = clock

    async def get_or_create(
        self,
        session: AsyncSession,
        *,
        merchant_id: str | None,
        type_: str,
        currency: str,
        livemode: bool,
    ) -> Account:
        """Return the one account for this (merchant, type, currency, livemode).

        Platform accounts pass ``merchant_id=None`` and are unique on
        ``pix_account_platform``.
        """
        if type_ not in NORMAL_BALANCE:
            raise ValidationError(
                f"unknown account type {type_!r}",
                account_type=type_,
            )

        existing = await self._accounts.find_one(
            session,
            merchant_id=merchant_id,
            type_=type_,
            currency=currency,
            livemode=livemode,
        )
        if existing is not None:
            return existing

        account = Account(
            id=new_id("acct"),
            merchant_id=merchant_id,
            type=type_,
            currency=currency,
            normal_balance=NORMAL_BALANCE[type_],
            status="active",
            livemode=livemode,
        )
        try:
            await self._accounts.add(session, account)
            await session.flush()
        except IntegrityError:
            # Another connection created it between the SELECT and the INSERT. The
            # unique index is the arbiter; re-read and use the winner.
            await session.rollback()
            winner = await self._accounts.find_one(
                session,
                merchant_id=merchant_id,
                type_=type_,
                currency=currency,
                livemode=livemode,
            )
            if winner is None:  # pragma: no cover - only reachable if the index is gone
                raise
            return winner

        logger.info(
            "account_created",
            account_id=account.id,
            merchant_id=merchant_id,
            account_type=type_,
            currency=currency,
            livemode=livemode,
        )
        return account

    async def bootstrap(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
    ) -> list[Account]:
        """Create the merchant's full account set for one currency.

        Idempotent by construction: it is four :meth:`get_or_create` calls.
        """
        accounts: list[Account] = []
        for type_ in MERCHANT_ACCOUNT_TYPES:
            accounts.append(
                await self.get_or_create(
                    session,
                    merchant_id=merchant_id,
                    type_=type_,
                    currency=currency,
                    livemode=livemode,
                )
            )
        logger.info(
            "accounts_bootstrapped",
            merchant_id=merchant_id,
            currency=currency,
            livemode=livemode,
            count=len(accounts),
        )
        return accounts

    async def freeze(
        self, session: AsyncSession, account_id: str, *, reason: str
    ) -> Account:
        """Set an account to ``frozen``. Postings against it then raise 409."""
        account = await self._accounts.get(session, account_id)
        if account is None:
            raise AccountNotFoundError(f"account {account_id} not found", account_id=account_id)
        if account.status == "closed":
            raise AccountFrozenError(
                "a closed account cannot be frozen",
                account_id=account_id,
                status=account.status,
            )
        account.status = "frozen"
        account.updated_at = self._clock.now()
        logger.warning("account_frozen", account_id=account_id, reason=reason)
        return account

    async def list_accounts(
        self,
        session: AsyncSession,
        *,
        merchant_id: str | None,
        currency: str | None,
        type_: str | None,
        livemode: bool,
    ) -> list[Account]:
        return await self._accounts.list_filtered(
            session,
            merchant_id=merchant_id,
            currency=currency,
            type_=type_,
            livemode=livemode,
        )
