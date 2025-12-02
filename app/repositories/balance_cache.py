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
