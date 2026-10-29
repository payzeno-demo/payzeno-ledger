"""Scheduled payouts — sweep every available balance to the merchant's bank, per schedule.

RFC platform-v2 §7: "Scheduled payouts sweep every non-zero currency balance to the default
bank account." For each balance with something available, a merchant on a ``daily``,
``weekly`` or ``monthly`` ``payout_schedule`` gets one payout per period, through
:meth:`PayoutService.create_payout` with no amount (the whole available balance) and no
bank account (the default for that currency, else the merchant's default — converting on
the way). ``manual`` merchants are left alone.

"Due" is "no payout out of this balance since the current period began": since midnight
for ``daily``, since Monday for ``weekly``, since the 1st for ``monthly``. Asked that way
rather than "is today the day", a clock that jumps from a Sunday to a Wednesday still pays
the weekly merchant this week, once.

Each payout is its own transaction. One merchant whose bank account is unusable, or whose
previous payout is still in flight, is logged and skipped; the rest of the pass carries
on. The advisory lock and ``pix_payout_in_flight`` inside ``create_payout`` are what make
four tasks running this at once safe — the due check here is only a filter.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, ClassVar, Final

from app.errors import PayzenoLedgerError
from app.logging import get_logger
from app.metrics import metrics
from app.ports import Clock, SessionFactory
from app.services.payouts import PayoutService
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

#: Hourly, like the scheduler that advances what this creates. The period check makes the
#: other twenty-three ticks a day no-ops for a daily merchant.
INTERVAL_SECONDS: Final[int] = 3600

#: Candidate balances per pass. A pass that hits it finishes the rest an hour later.
MAX_BALANCES_PER_PASS: Final[int] = 500

SWEPT_SCHEDULES: Final[frozenset[str]] = frozenset({"daily", "weekly", "monthly"})
SWEEPABLE_MERCHANT_STATUSES: Final[frozenset[str]] = frozenset({"active"})


def period_start(schedule: str, now: datetime) -> datetime:
    """Start of the payout period ``now`` falls in, for a sweepable ``schedule``."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if schedule == "weekly":
        return midnight - timedelta(days=midnight.weekday())
    if schedule == "monthly":
        return midnight.replace(day=1)
    return midnight


class PayoutSweepJob(PeriodicJob):
    """Create the scheduled payout for every balance that is due one."""

    name: ClassVar[str] = "payout_sweep"

    def __init__(
        self,
        sessions: SessionFactory,
        payouts: PayoutService,
        repositories: Any,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._payouts = payouts
        self._repos = repositories
        self._clock = clock

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        started = time.monotonic()
        now = self._clock.now()
        created = 0
        skipped = 0

        async with self._sessions.begin() as session:
            balances = await self._repos.balance_cache.list_payable(
                session, limit=MAX_BALANCES_PER_PASS
            )
            merchants = {
                merchant_id: await self._repos.merchants.get(session, merchant_id)
                for merchant_id in {row.merchant_id for row in balances}
            }

        for balance in balances:
            merchant = merchants.get(balance.merchant_id)
            if (
                merchant is None
                or merchant.status not in SWEEPABLE_MERCHANT_STATUSES
                or merchant.payout_schedule not in SWEPT_SCHEDULES
            ):
                continue
            since = period_start(merchant.payout_schedule, now)
            try:
                if await self._sweep_one(balance, since=since):
                    created += 1
            except PayzenoLedgerError as exc:
                # Raised out of the payout's own transaction, which has rolled back.
                skipped += 1
                logger.info(
                    "payout_sweep_skipped",
                    merchant_id=balance.merchant_id,
                    currency=balance.currency,
                    livemode=balance.livemode,
                    code=exc.code,
                    reason=str(exc)[:200],
                )
                metrics.increment("PayoutSweepSkipped", code=exc.code)

        logger.info(
            "payout_sweep_pass",
            candidates=len(balances),
            created=created,
            skipped=skipped,
        )
        return self._result(started, created)

    async def _sweep_one(self, balance: Any, *, since: datetime) -> bool:
        """One balance, one transaction. False when it was already paid this period."""
        async with self._sessions.begin() as session:
            last = await self._repos.payouts.last_created_at(
                session,
                merchant_id=balance.merchant_id,
                currency=balance.currency,
                livemode=balance.livemode,
            )
            if last is not None and last >= since:
                return False
            payout = await self._payouts.create_payout(
                session,
                merchant_id=balance.merchant_id,
                req={"currency": balance.currency, "livemode": balance.livemode},
            )
            logger.info(
                "payout_swept",
                payout_id=payout.id,
                merchant_id=payout.merchant_id,
                amount_minor=payout.amount_minor,
                currency=payout.currency,
                source_currency=payout.source_currency,
                source_amount_minor=payout.source_amount_minor,
            )
        metrics.increment("PayoutSwept", currency=balance.currency)
        return True
