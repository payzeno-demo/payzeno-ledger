"""``ledger_entry`` data access — append-only, and the source of every balance number.

There is no update method and no delete method on this class, and adding one would not
work anyway: ``trg_ledger_entry_immutable`` (migration ``0004``) raises ``P0001`` on any
``UPDATE`` or ``DELETE`` against the table. Corrections are compensating ``reversal``
transactions.

Two queries in here are arc PERF's (`docs/adr/0010-partial-indexes-on-hot-paths.md`):
:meth:`LedgerEntryRepository.sum_by_account_and_purpose` and
:meth:`LedgerEntryRepository.trial_balance_by_currency` both drive off
``ix_ledger_entry_account_created``, added in migration ``0015`` in month 7 after the
balance endpoint's p99 went past four seconds.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.ledger_entry import LedgerEntry
from app.models.ledger_transaction import LedgerTransaction
from app.repositories.base import BaseRepository

#: The account types that make up each balance bucket the API reports. Kept here rather
#: than in the service because the SQL is what needs them; `BalanceService` reads the
#: dict this module returns and never restates the mapping.
BUCKET_ACCOUNT_TYPES: dict[str, str] = {
    "merchant_payable": "merchant_payable",
    "merchant_receivable": "merchant_pending",
    "reserve": "merchant_reserve",
    "chargeback_liability": "merchant_disputed",
}

#: Postgres ``date_trunc`` units, by the interval name the API accepts.
_TRUNC_UNIT: dict[str, str] = {"hour": "hour", "day": "day"}


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    """Debits and credits for one ``(currency, livemode)`` pair."""

    currency: str
    livemode: bool
    debit_minor: int
    credit_minor: int

    @property
    def delta_minor(self) -> int:
        """Debits minus credits. Zero, or the ledger is broken."""
        return self.debit_minor - self.credit_minor


@dataclass(frozen=True, slots=True)
class TrialBalanceTotals:
    """Currency-wide totals, plus the per-``livemode`` rows behind them.

    ``LedgerAuditService`` reads ``.debit_minor`` / ``.credit_minor`` off this object;
    the invariant test iterates it to assert every constituent row nets to zero as well,
    because a live overstatement exactly cancelled by a test-mode understatement would
    otherwise pass. Hence both shapes — it is one query and two readers, not two queries.
    """

    currency: str
    debit_minor: int
    credit_minor: int
    rows: tuple[TrialBalanceRow, ...]

    def __iter__(self) -> Any:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def delta_minor(self) -> int:
        return self.debit_minor - self.credit_minor


@dataclass(frozen=True, slots=True)
class BalanceBucketRow:
    """One time bucket of movement on an account, for the balance history endpoint."""

    bucket_start: dt.datetime
    delta_minor: int


class LedgerEntryRepository(BaseRepository[LedgerEntry]):
    """Reads ``ledger_entry`` and writes it exactly once per row, never again."""

    model: ClassVar[type[LedgerEntry]] = LedgerEntry

    def _default_order(self) -> ColumnElement[Any]:
        return LedgerEntry.id

    async def list_for_transaction(
        self, session: AsyncSession, transaction_id: str
    ) -> list[LedgerEntry]:
        """Every leg of one transaction, in the order the posting rule built them.

        Ordered by ``sequence`` and not by id: the id is a ULID and two legs written
        inside the same millisecond are not reliably ordered by it, and a reversal that
        mirrors the legs in the wrong order still balances but reads as nonsense in the
        ops CLI.
        """
        stmt = (
            select(LedgerEntry)
            .where(LedgerEntry.transaction_id == transaction_id)
            .order_by(LedgerEntry.sequence)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def sum_by_account_and_purpose(
        self,
        session: AsyncSession,
        purpose: str | None = None,
        totals = dict.fromkeys(BUCKET_ACCOUNT_TYPES.values(), 0)
        for account_type, amount in (await session.execute(stmt)).all():
            totals[BUCKET_ACCOUNT_TYPES[account_type]] = int(amount or 0)
        return totals

    async def sum_by_bucket(
        self,
        session: AsyncSession,
        *,
        account_id: str,
        from_: dt.datetime,
        to: dt.datetime,
        interval: str = "day",
    ) -> list[BalanceBucketRow]:
        """Movement on one account, bucketed by hour or day, oldest first.

        Backs ``GET /internal/v1/balances/{merchantId}/history``. The running balance is
        accumulated by the caller rather than by a window function, because the endpoint
        also needs the opening balance, which comes from a different query anyway.
        stmt = (
            select(bucket, func.coalesce(signed, 0))
            .where(LedgerEntry.account_id == account_id)
            .where(LedgerEntry.created_at >= from_)
            .where(LedgerEntry.created_at < to)
            .group_by(bucket)
            .order_by(bucket)
        )
        return [
            BalanceBucketRow(bucket_start=row[0], delta_minor=int(row[1] or 0))
            for row in (await session.execute(stmt)).all()
        ]

    async def trial_balance_by_currency(
        self,
        session: AsyncSession,
        *,
        currency: str,
        rows = tuple(
            TrialBalanceRow(
                currency=currency,
                livemode=bool(row[0]),
                debit_minor=int(row[1] or 0),
                credit_minor=int(row[2] or 0),
            )
            for row in (await session.execute(stmt)).all()
        )
        return TrialBalanceTotals(
            currency=currency,
            debit_minor=sum(row.debit_minor for row in rows),
            credit_minor=sum(row.credit_minor for row in rows),
            rows=rows,
        )

    async def sample_unbalanced_transactions(
        self,
        session: AsyncSession,
        *,
        currency: str,
        as_of: dt.datetime | None = None,
        """How many legs an account carries. Used by the ops CLI before a freeze."""
        stmt = (
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.account_id == account_id)
        )
        return int((await session.execute(stmt)).scalar_one())
