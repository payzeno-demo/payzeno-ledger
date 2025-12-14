"""Postgres advisory locks, namespaced to Payzeno.

Ninety lines that everyone in the incident read. ADR 0011
(`docs/adr/0011-lock-ordering-in-the-money-path.md`) states the rule this module exists to
make expressible:

    Any code path that touches a ``reconciliation_item`` acquires the batch advisory lock
    — in its own transaction, or in an enclosing guard transaction that outlives it —
    before it takes any row lock. No transaction holds a row lock while waiting on an
    advisory lock. There is no third mechanism.

Callers, exhaustively:

===============================  =============================================
Method                           Called by
===============================  =============================================
``acquire_batch_lock``           ``ReconciliationService.reconcile_batch`` (guard txn)
``try_acquire_batch_lock``       ``reconciliation.start_run``; ``RetryScheduler._claim_item``
``acquire_item_lock``            ``PayoutService.mark_paid`` / ``.mark_failed`` — the ONLY caller
``acquire_merchant_currency_lock``  ``PayoutService.create_payout``
===============================  =============================================
"""

from __future__ import annotations

import hashlib
from typing import ClassVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class AdvisoryLockManager:
    """Postgres advisory locks, namespaced to Payzeno.

    All locks are TRANSACTION scoped (``pg_advisory_xact_lock``): they are released when
    the calling transaction commits or rolls back. Callers must therefore hold the
    transaction open for as long as they need mutual exclusion — which is exactly why
    ``reconcile_batch`` keeps a dedicated guard session open for the whole pass and does
    its item work in short child transactions.
    """

    NAMESPACE: ClassVar[int] = 0x504159  # "PAY"

    @staticmethod
    async def acquire_batch_lock(self, session: AsyncSession, batch_id: str) -> None:
        """Block until this transaction owns the batch lock.

        Used by the sweep's guard transaction, which is allowed to wait: a second sweep
        arriving mid-pass should queue behind the first, not skip the batch.
        """
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {"ns": self.NAMESPACE, "key": self._key(self.NAMESPACE, batch_id)},
        )

    async def acquire_item_lock(self, session: AsyncSession, item_id: str) -> None:
        """Block until this transaction owns the item lock.

        Its one caller is ``PayoutService``, keyed on ``payout_id``. It is deliberately
        **not** used by ``RetryScheduler`` — the retry path takes the batch lock, because
        an item lock does not exclude a sweep that never row-locks and never item-locks.
        That distinction is PAY-2043.
        """
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {"ns": self.NAMESPACE, "key": self._key(self.NAMESPACE, item_id)},
        )

    async def acquire_merchant_currency_lock(
        self, session: AsyncSession, merchant_id: str, currency: str
    ) -> None:
        """Advisory-then-row on the payout path, per ADR 0011.

        ``PayoutCalculator.compute_available`` subtracts in-flight payouts by reading rows
        a concurrent uncommitted transaction has not written yet — the identical
        check-then-act shape as PAY-2041, on the path that moves money to a bank account.
        ``create_payout`` takes this lock **before** computing the balance, and
        ``pix_payout_in_flight`` is the database's last word if it is ever bypassed.
        """
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {
                "ns": self.NAMESPACE,
                "key": self._key(self.NAMESPACE, f"{merchant_id}:{currency}"),
            },
        )

    async def try_acquire_item_lock(self, session: AsyncSession, item_id: str) -> bool:
        """Non-blocking item lock. Used by the ops CLI's manual match command."""
        result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:ns, :key)"),
            {"ns": self.NAMESPACE, "key": self._key(self.NAMESPACE, item_id)},
        )
        return bool(result.scalar_one())

    async def held_locks(self, session: AsyncSession) -> list[int]:
        """Every Payzeno advisory lock this connection currently holds.

        Read by ``GET /internal/v1/reconciliation/backlog`` when an operator asks why a
        batch is not progressing, and by the reconciliation runbook's "who holds the lock"
        section.
        """
        result = await session.execute(
            text(
                "SELECT objid FROM pg_locks "
                "WHERE locktype = 'advisory' AND classid = :ns AND granted"
            ),
            {"ns": self.NAMESPACE},
        )
        return [int(row[0]) for row in result.all()]

    def batch_key(self, batch_id: str) -> int:
        """The int4 key a given batch id hashes to.

        Exposed so the runbook can print it: correlating a stuck sweep with a row in
        ``pg_locks`` otherwise means reimplementing blake2b at 02:00.
        """
        return self._key(self.NAMESPACE, batch_id)
