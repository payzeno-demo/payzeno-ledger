"""Merchant balance reads.

Two sources of truth, and they are supposed to agree:

* ``merchant_balance_cache`` — maintained by ``LedgerPoster.post`` in the same
  transaction as the entries it writes, so it can never lag a committed posting. This
  is what ``GET /internal/v1/balances/{merchantId}`` serves.
* ``ledger_entry`` — the actual double-entry record. Summing it is correct and slow;
  ``LedgerAuditJob`` does it nightly and compares.

Arc PERF touched this module: the history endpoint used to sum entries per bucket in a
loop, which on a merchant with 400k entries was a 12-second request. It now leans on
``ix_ledger_entry_account_created``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.errors import ValidationError
from app.logging import get_logger
from app.ports import Clock, SessionFactory
from app.repositories.account import AccountRepository
from app.repositories.balance_cache import MerchantBalanceCacheRepository
from app.repositories.ledger_entry import LedgerEntryRepository

logger = get_logger(__name__)

#: Bucket widths accepted by `GET /balances/{merchantId}/history?interval=`.
INTERVALS: dict[str, timedelta] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
}

#: Maximum buckets one history request may produce. A merchant asking for a year of
#: hourly buckets gets a 422 rather than 8,760 rows and a timeout.
MAX_BUCKETS = 400


class BalanceService:
    """Serves balance reads for payzeno-api and payzeno-billing-legacy.

    Both callers hit this on the request path — the legacy Java service calls
    ``LedgerClient.getBalance`` inside invoice generation — so everything here is a
    single indexed read. No sums over ``ledger_entry`` on the request path, ever.
    """

    def __init__(
        self,
        sessions: SessionFactory,
        balances: MerchantBalanceCacheRepository,
        entries: LedgerEntryRepository,
        accounts: AccountRepository,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._balances = balances
        self._entries = entries
        self._accounts = accounts
        self._clock = clock

    async def get_balance(
        self,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        """Return the merchant's balance breakdown for one currency.

        ``as_of`` in the past cannot be served from the cache — the cache holds current
        state only — so a historical read falls through to the entry sum. That is the
        slow path and it is deliberately not the default.
        """
        async with self._sessions.begin() as session:
            if as_of is not None and as_of < self._clock.now():
                return await self._historical_balance(
                    session,
                    merchant_id=merchant_id,
                    currency=currency,
                    livemode=livemode,
                    as_of=as_of,
                )

            cached = await self._balances.get(
                session, merchant_id=merchant_id, currency=currency, livemode=livemode
            )

        if cached is None:
            # A merchant with no postings yet. Zeroes are the honest answer; inventing
            # a row here would make the audit job's drift check fire on every new
            # merchant.
            return _empty_balance(merchant_id, currency, livemode, self._clock.now())

        return {
            "merchant_id": merchant_id,
            "currency": currency,
            "livemode": livemode,
            "available_minor": cached.available_minor,
            "pending_minor": cached.pending_minor,
            "reserved_minor": cached.reserved_minor,
            "disputed_minor": cached.disputed_minor,
            "negative_balance_minor": cached.negative_balance_minor,
            "as_of": cached.computed_at.isoformat(),
        }

    async def get_balance_history(
        self,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
        from_: datetime,
        to: datetime,
        interval: str = "day",
    ) -> dict[str, object]:
        """Bucketed movement of the merchant's payable account."""
        width = INTERVALS.get(interval)
        if width is None:
            raise ValidationError(
                f"unsupported interval {interval!r}",
                interval=interval,
                buckets=bucket_count,
                max_buckets=MAX_BUCKETS,
                interval=interval,
            )

        async with self._sessions.begin() as session:
            account = await self._accounts.find_one(
                session,
                merchant_id=merchant_id,
                livemode=livemode,
            )
            if account is None:
                return {"merchant_id": merchant_id, "currency": currency, "buckets": []}

            rows = await self._entries.sum_by_bucket(
                session,
                to=to,
                interval=interval,
            )

        running = 0
        buckets = []
        for row in rows:
            running += row.delta_minor
            buckets.append(
                {
                    "bucket_start": row.bucket_start.isoformat(),
                    "delta_minor": row.delta_minor,
                    "balance_minor": running,
                }
            )

        logger.info(
            "balance_history_read",
            merchant_id=merchant_id,
            currency=currency,
            interval=interval,
            buckets=len(buckets),
        )
        return {
            "merchant_id": merchant_id,
            "currency": currency,
            "livemode": livemode,
            "interval": interval,
            "buckets": buckets,
        }

    async def _historical_balance(
        self,
        session,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
        as_of: datetime,
    ) -> dict[str, object]:
        totals = await self._entries.sum_by_account_and_purpose(
            session,
            merchant_id=merchant_id,
            currency=currency,
            livemode=livemode,
            as_of=as_of,
        )
        return {
            "merchant_id": merchant_id,
            "currency": currency,
            "livemode": livemode,
            "available_minor": totals.get("merchant_payable", 0),
            "pending_minor": totals.get("merchant_pending", 0),
            "reserved_minor": totals.get("merchant_reserve", 0),
            "disputed_minor": totals.get("merchant_disputed", 0),
            "negative_balance_minor": min(0, totals.get("merchant_payable", 0)),
            "as_of": as_of.isoformat(),
        }


def _empty_balance(
    merchant_id: str, currency: str, livemode: bool, now: datetime
) -> dict[str, object]:
    return {
        "merchant_id": merchant_id,
        "currency": currency,
        "livemode": livemode,
        "available_minor": 0,
        "pending_minor": 0,
        "reserved_minor": 0,
        "disputed_minor": 0,
        "negative_balance_minor": 0,
        "as_of": now.isoformat(),
    }
