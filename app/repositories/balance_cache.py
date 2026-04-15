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
        pending_delta: int = 0,
        reserved_delta: int = 0,
        disputed_delta: int = 0,
        last_transaction_id: str | None = None,
        computed_at: dt.datetime | None = None,
    ) -> None:
        """Apply one posting's signed deltas to the cached balance.

        ``INSERT ... ON CONFLICT (merchant_id, currency, livemode) DO UPDATE SET
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
        """
        stamp = computed_at or dt.datetime.now(dt.UTC)
        stmt = pg_insert(MerchantBalanceCache).values(
            merchant_id=merchant_id,
            currency=currency,
            livemode=livemode,
            available_minor=available_delta,
            pending_minor=pending_delta,
            reserved_minor=reserved_delta,
            disputed_minor=disputed_delta,
            negative_balance_minor=max(0, -available_delta),
            last_transaction_id=last_transaction_id,
            computed_at=stamp,
            updated_at=stamp,
        )
        updated_available = (
            MerchantBalanceCache.available_minor + stmt.excluded.available_minor
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["merchant_id", "currency", "livemode"],
            set_={
                "available_minor": updated_available,
                "pending_minor": (
                    MerchantBalanceCache.pending_minor + stmt.excluded.pending_minor
                ),
                "reserved_minor": (
                    MerchantBalanceCache.reserved_minor + stmt.excluded.reserved_minor
                ),
                "disputed_minor": (
                    MerchantBalanceCache.disputed_minor + stmt.excluded.disputed_minor
                ),
                "negative_balance_minor": func.greatest(0, -updated_available),
                "last_transaction_id": stmt.excluded.last_transaction_id,
                "computed_at": stmt.excluded.computed_at,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await session.execute(stmt)

    async def list_stale(
        self,
        session: AsyncSession,
        *,
        limit: int = 500,
        older_than: dt.datetime | None = None,
    ) -> list[MerchantBalanceCache]:
        """Cached balances least recently recomputed, oldest first.

        The audit job walks these and recomputes each from ``ledger_entry``. Ordered by
        ``computed_at`` under ``ix_merchant_balance_cache_computed`` so a nightly run with
        a budget of 500 rows eventually covers every merchant rather than re-checking the
        same alphabetical prefix forever.
        """
        stmt = (
            select(MerchantBalanceCache)
            .order_by(MerchantBalanceCache.computed_at)
            .limit(limit)
        )
        if older_than is not None:
            stmt = stmt.where(MerchantBalanceCache.computed_at < older_than)
        return list((await session.execute(stmt)).scalars().all())

    async def list_negative(
        self, session: AsyncSession, *, limit: int = 200
    ) -> list[MerchantBalanceCache]:
        """Merchants who owe Payzeno money, largest shortfall first.

        Uses ``pix_merchant_balance_cache_negative``. ``NegativeBalanceJob`` reads this
        daily; past a threshold it opens a ``debit_ach`` payout that pulls funds back, and
        below the threshold it files a risk support task.
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
