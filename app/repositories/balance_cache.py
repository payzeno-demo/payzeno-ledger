"""``merchant_balance_cache`` data access.

The cache is maintained by ``LedgerPoster.post`` **in the same transaction as the entries
it describes**, so it can never lag a committed posting. That is the whole design: a cache
updated by a follow-up job is a cache that is wrong for as long as the job is behind, and
the number it is wrong about is the one a payout is drawn against.

``LedgerAuditJob`` invariant (3) recomputes from ``ledger_entry`` nightly and compares. The
drift alarm on this table — ``LedgerBalanceCacheDrift`` — is the single alarm that fired
during the sev1 in month 9, and it paged the wrong person, because a drifting balance cache
looks like a caching bug and not like a double settlement.

Primary key is ``(merchant_id, currency, livemode)``, so :meth:`get` here takes three
arguments rather than an id.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.merchant_balance_cache import MerchantBalanceCache
from app.repositories.base import BaseRepository


class MerchantBalanceCacheRepository(BaseRepository[MerchantBalanceCache]):
    """Reads and incrementally updates the merchant balance cache."""

    model: ClassVar[type[MerchantBalanceCache]] = MerchantBalanceCache
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        return MerchantBalanceCache.computed_at

    async def get(  # type: ignore[override]
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
    ) -> MerchantBalanceCache | None:
        """Fetch one cached balance by its composite key, or ``None``.

        ``None`` means the merchant has never had a posting in this currency. The balance
        service answers zeroes rather than creating a row: inventing one here would make
        the drift check fire on every merchant who has signed up and not yet traded.
        """
        return await session.get(
            MerchantBalanceCache, (merchant_id, currency, livemode)
        )

    async def apply_delta(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
        available_delta: int = 0,
        reserved_delta: int = 0,
        disputed_delta: int = 0,
        available_minor = merchant_balance_cache.available_minor + EXCLUDED...`` — the
        arithmetic happens **in the database**, not in Python.

        That matters. Read-modify-write in the process would lose an increment whenever
        two postings for the same merchant commit concurrently, and unlike the settlement
        race this one would not even be visible: the balance would simply be a little
        wrong, in a direction nobody can predict, and the nightly drift check would report
        it as "cache drift" a day later. Adding in SQL makes concurrent postings commute.

        ``negative_balance_minor`` is derived, not passed: it is the shortfall when
        ``available_minor`` has gone below zero, and computing it anywhere other than
        beside the number it is derived from is how the two disagree.
        updated_available = (
            MerchantBalanceCache.available_minor + stmt.excluded.available_minor
        )
        """
        stmt = (
            select(MerchantBalanceCache)
            .where(MerchantBalanceCache.negative_balance_minor > 0)
            .order_by(MerchantBalanceCache.negative_balance_minor.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def overwrite(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
        available_minor: int,
        pending_minor: int,
        reserved_minor: int,
        disputed_minor: int,
        computed_at: dt.datetime,
    ) -> MerchantBalanceCache:
        """Replace a cached balance with a freshly recomputed one.

        The repair half of the drift check, and the **only** write here that is not a
        delta. Deliberately not called automatically: an unexplained drift is a symptom,
        and silently correcting it destroys the evidence. The ops CLI exposes it as
        ``balance-cache repair`` and the runbook says to capture the before value first.
        """
        row = await self.get(
            session, merchant_id=merchant_id, currency=currency, livemode=livemode
        )
        if row is None:
            row = MerchantBalanceCache(
                merchant_id=merchant_id,
                currency=currency,
                livemode=livemode,
                computed_at=computed_at,
                updated_at=computed_at,
            )
            session.add(row)
        row.available_minor = available_minor
        row.pending_minor = pending_minor
        row.reserved_minor = reserved_minor
        row.disputed_minor = disputed_minor
        row.negative_balance_minor = max(0, -available_minor)
        row.computed_at = computed_at
        row.updated_at = computed_at
        await session.flush()
        return row
