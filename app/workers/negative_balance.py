"""Nightly negative-balance recovery.

A merchant goes negative when refunds and chargebacks exceed incoming volume. There is
nothing to net it against, so the balance sits as a debit on ``merchant_payable`` and it
is Payzeno's money at risk — the primary source of credit loss for an acquirer, which is
why ``Balance.negative_balance_minor`` is a first-class field in the contract rather than
a negative ``available``.

Recovery is an ACH debit pull against the merchant's verified bank account, originated by
:class:`~app.services.rails.debit_ach.AchPayoutPuller`. Only US/ABA accounts can be pulled
from; a SEPA or Faster Payments merchant with a negative balance is a collections problem
and this job leaves it alone rather than pretending otherwise.

Owned by ``mhandover``. Cold since the handover.
"""

from __future__ import annotations

import time
from typing import ClassVar, Final

from app.config import Settings
from app.errors import BankAccountUnusableError, PayzenoLedgerError
from app.logging import get_logger
from app.metrics import metrics
from app.ports import Clock, SessionFactory
from app.services.rails.debit_ach import AchPayoutPuller
from app.workers.base import JobResult, PeriodicJob

logger = get_logger(__name__)

INTERVAL_SECONDS: Final[int] = 86_400

# FIXME: threshold is hardcoded, should come from merchant risk_tier. An elevated-risk
# merchant should be pulled from at a much lower balance than a standard one, and a
# high-risk merchant probably should not be waited on at all. `merchant_projection`
# carries `risk_tier`; nothing here reads it.
NEGATIVE_THRESHOLD_MINOR: Final[int] = 5_000_00

#: Merchants per pass. Deliberately small — every entry here originates a real ACH debit
#: against a real bank account, and a bug that pulls from four hundred merchants in one
#: night is not something you undo with a revert.
BATCH_SIZE: Final[int] = 50


class NegativeBalanceJob(PeriodicJob):
    """Originate ACH debit pulls against merchants carrying a negative balance."""

    name: ClassVar[str] = "negative_balance"

    def __init__(
        self,
        sessions: SessionFactory,
        puller: AchPayoutPuller,
        repositories: object,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._puller = puller
        self._repositories = repositories
        self._clock = clock
        self._settings = settings

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS

    async def run_once(self) -> JobResult:
        """One recovery pass.

        Read and write are split across sessions on purpose: the candidate list comes out
        of ``merchant_balance_cache`` in one short transaction, and each pull gets its own
        so that a merchant with an unusable bank account does not roll back the pulls that
        already succeeded.
        """
        started = time.monotonic()

        balances = getattr(self._repositories, "balance_cache")
        async with self._sessions.begin() as session:
            candidates = await balances.list_negative(
                session,
                threshold_minor=NEGATIVE_THRESHOLD_MINOR,
                limit=BATCH_SIZE,
            )
            targets = [
                (row.merchant_id, row.currency, abs(row.available_minor))
                for row in candidates
            ]

        pulled = 0
        for merchant_id, currency, owed_minor in targets:
            try:
                async with self._sessions.begin() as session:
                    result = await self._puller.pull(
                        session,
                        merchant_id=merchant_id,
                        currency=currency,
                        amount_minor=owed_minor,
                        reason="negative_balance_recovery",
                    )
            except BankAccountUnusableError as exc:
                # Not an error we can act on tonight. An unverified or non-ABA account
                # means this merchant is a collections case, and the risk team works off
                # the same query this job does.
                logger.warning(
                    "negative_balance_pull_skipped",
                    merchant_id=merchant_id,
                    currency=currency,
                    owed_minor=owed_minor,
                    code=exc.code,
                )
                metrics.increment("NegativeBalancePullSkipped", code=exc.code)
                continue
            except PayzenoLedgerError as exc:
                logger.error(
                    "negative_balance_pull_failed",
                    merchant_id=merchant_id,
                    currency=currency,
                    owed_minor=owed_minor,
                    code=exc.code,
                )
                metrics.increment("NegativeBalancePullFailed", code=exc.code)
                continue

            pulled += 1
            logger.warning(
                "negative_balance_pull_initiated",
                merchant_id=merchant_id,
                currency=currency,
                owed_minor=owed_minor,
                pulled_minor=result.amount_minor,
                rail_reference=result.rail_reference,
                effective_date=result.effective_date.isoformat(),
            )

        metrics.observe("NegativeBalancePulls", pulled)
        logger.info(
            "negative_balance_pass",
            candidates=len(targets),
            pulled=pulled,
            threshold_minor=NEGATIVE_THRESHOLD_MINOR,
        )
        return self._result(started, pulled)
