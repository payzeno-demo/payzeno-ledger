"""``account`` data access — the chart of accounts.

One caller inserts through here and one only: ``AccountResolver.get_or_create``
(`domain-model.md` §6). Everything else on the money path resolves an account and never
creates one, which is why :meth:`AccountRepository.find_one` exists as a separate method
from :meth:`~app.repositories.base.BaseRepository.get` — the resolver looks accounts up by
their natural key ``(merchant_id, type, currency, livemode)``, not by id, and the unique
index behind that natural key is what makes calling the resolver twice a no-op.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AccountNotFoundError
from app.models.account import Account
from app.repositories.base import BaseRepository


class AccountRepository(BaseRepository[Account]):
    """Reads and writes ``account`` rows.

    Stateless. Every method takes the session it should run on, because the merchant
    bootstrap path opens twelve accounts inside the consumer's transaction while the
    posting path resolves one inside the poster's, and both use this same instance.
    """

    model: ClassVar[type[Account]] = Account
    not_found_error: ClassVar[type[AccountNotFoundError]] = AccountNotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        """Newest first. Nothing pages accounts in anger — a merchant has at most a dozen."""
        return Account.id

    async def find_one(
        self,
        session: AsyncSession,
        *,
        merchant_id: str | None,
        type_: str,
        currency: str,
        livemode: bool,
    ) -> Account | None:
        """Look an account up by its natural key, or return ``None``.

        The natural key is ``(merchant_id, type, currency, livemode)`` and it is backed by
        ``uq_account_merchant_type_currency_livemode``. Platform accounts carry a null
        ``merchant_id`` and are unique under the partial index ``pix_account_platform``
        instead, so the ``IS NULL`` branch below is not a nicety: ``merchant_id = NULL``
        is never true in SQL and this method would return ``None`` for every platform
        account, which would make ``AccountResolver`` insert a second ``cash`` account per
        currency on every posting until the partial index rejected it.
        """
        stmt = (
            select(Account)
            .where(Account.type == type_)
            .where(Account.currency == currency)
            .where(Account.livemode == livemode)
        )
        if merchant_id is None:
            stmt = stmt.where(Account.merchant_id.is_(None))
        else:
            stmt = stmt.where(Account.merchant_id == merchant_id)
        return (await session.execute(stmt)).scalars().first()

    async def list_for_merchant(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str | None = None,
        livemode: bool | None = None,
    ) -> list[Account]:
        """Every account belonging to one merchant, optionally narrowed by currency.

        Used by ``GET /internal/v1/accounts`` and by the ops CLI's ``accounts`` command.
        Ordered by type so a printed chart of accounts is stable between runs.
        """
        stmt = select(Account).where(Account.merchant_id == merchant_id)
        if currency is not None:
            stmt = stmt.where(Account.currency == currency)
        if livemode is not None:
            stmt = stmt.where(Account.livemode == livemode)
        stmt = stmt.order_by(Account.type, Account.currency)
        return list((await session.execute(stmt)).scalars().all())

    async def list_filtered(
        self,
        session: AsyncSession,
        *,
        merchant_id: str | None = None,
        currency: str | None = None,
        type_: str | None = None,
        livemode: bool | None = None,
        limit: int = 200,
    ) -> list[Account]:
        """The general filtered read behind ``GET /internal/v1/accounts``.

        Every filter is optional and a filter left at ``None`` is simply not applied.
        ``merchant_id=None`` therefore means "do not filter by merchant" and **not**
        "platform accounts only" — use :meth:`find_one` when the null merchant is the
        thing being asked for. That distinction has bitten a reviewer at least once, hence
        this paragraph.
        """
        stmt = select(Account)
        if merchant_id is not None:
            stmt = stmt.where(Account.merchant_id == merchant_id)
        if currency is not None:
            stmt = stmt.where(Account.currency == currency)
        if type_ is not None:
            stmt = stmt.where(Account.type == type_)
        if livemode is not None:
            stmt = stmt.where(Account.livemode == livemode)
        stmt = stmt.order_by(Account.merchant_id, Account.type).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def list_platform_accounts(
        self, session: AsyncSession, *, currency: str, livemode: bool
    ) -> list[Account]:
        """The platform-level accounts for one currency (``merchant_id IS NULL``).

        ``cash``, the three expense accounts and ``platform_fee_revenue`` live here. The
        nightly trial balance walks them per currency.
        """
        stmt = (
            select(Account)
            .where(Account.merchant_id.is_(None))
            .where(Account.currency == currency)
            .where(Account.livemode == livemode)
            .order_by(Account.type)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def set_status(
        self, session: AsyncSession, account_id: str, *, status: str
    ) -> Account:
        """Freeze, close or re-activate one account.

        Raises :class:`AccountNotFoundError` when the id is unknown — a freeze against a
        typo'd id must not silently succeed and report "frozen" to the operator. Posting
        to a row left in ``frozen`` or ``closed`` raises ``AccountFrozenError`` in
        ``LedgerPoster.post``; that is a 409, because a frozen account is a deliberate
        business state and not a server fault.
        """
        account = await self.get_or_raise(session, account_id)
        account.status = status
        await session.flush()
        return account
