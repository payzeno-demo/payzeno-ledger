"""``ledger_transaction`` data access.

Two methods here answer the same question and only one of them is safe, which is the
whole of PAY-2041:

* :meth:`~LedgerTransactionRepository.find_by_idempotency_key` is a plain ``SELECT``. It
  reads committed rows and cannot see a concurrent transaction's uncommitted INSERT, so a
  caller that used it to decide whether to post was doing check-then-act around an
  irreversible side effect. **It is no longer on the money path** — the ops CLI and one
  audit query still call it — and it is kept, with this comment, because deleting it would
  erase the evidence.
* :meth:`~LedgerTransactionRepository.claim_idempotency_key` is
  ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING`` inside the caller's
  transaction. Either you inserted the row or somebody else did, and you find out inside
  one statement. Added by PR #172 alongside migration ``0020``, which is what made the
  unique index exist for the ``ON CONFLICT`` to name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ids import new_id
from app.errors import TransactionNotFoundError
from app.models.ledger_transaction import LedgerTransaction
from app.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """The outcome of :meth:`LedgerTransactionRepository.claim_idempotency_key`.

    ``created`` is False when somebody else owns the key. The caller then has the winner's
    transaction id without a second round trip, which is what lets ``SettlementPoster``
    emit ``settlement.duplicate_detected`` naming both rows.
    """

    transaction_id: str
    created: bool
    fingerprint_matches: bool = True


@dataclass(frozen=True, slots=True)
class DuplicateKeyRow:
    """One idempotency key that appears on more than one transaction.

    Produced by :meth:`LedgerTransactionRepository.list_duplicate_idempotency_keys` and
    consumed by ``LedgerAuditService.check_duplicate_settlements`` — invariant (1) of
    `data-model.md` §6, the check that did not exist on the night of the incident because
    every check that did exist was a *balance* check and a duplicate settlement balances
    perfectly.
    """

    idempotency_key: str
    count: int
    currency: str
    sample_transaction_id: str


class LedgerTransactionRepository(BaseRepository[LedgerTransaction]):
    """Reads and writes ``ledger_transaction`` rows.

    Only ``LedgerPoster`` inserts through here on the business path. Everything else
    reads.
    """

    model: ClassVar[type[LedgerTransaction]] = LedgerTransaction
    not_found_error: ClassVar[type[TransactionNotFoundError]] = TransactionNotFoundError

    def _default_order(self) -> ColumnElement[Any]:
        """Paginate on the id, which is a ULID and therefore time-ordered already."""
        return LedgerTransaction.id

    async def find_by_idempotency_key(
        self, session: AsyncSession, key: str
    ) -> LedgerTransaction | None:
        """Return the transaction holding this idempotency key, if one is committed.

        ``.scalars().first()`` and not ``.one_or_none()``: the ``0007`` backfill produced
        genuine duplicate keys for pre-existing auth/capture pairs, and ``one_or_none()``
        would turn a six-year-old data artefact into a 500 on a read path. It also means
        this method never notices when duplicates appear — which is exactly what happened
        for twenty-two minutes in month 9.

        This used to be the settlement guard. It is not any more; see
        :meth:`claim_idempotency_key` and PR #172.
        """
        stmt = select(LedgerTransaction).where(LedgerTransaction.idempotency_key == key)
        return (await session.execute(stmt)).scalars().first()

    async def claim_idempotency_key(
        self,
        session: AsyncSession,
        *,
        key: str,
        purpose: str,
        merchant_id: str | None,
        currency: str,
        reference_type: str,
        reference_id: str,
        created_by: str,
        livemode: bool,
        request_fingerprint: str,
    ) -> IdempotencyClaim:
        """Atomically claim ``key``, inserting the transaction row if it is free.

        One statement, in the caller's transaction:

        ``INSERT INTO ledger_transaction (...) VALUES (...) ON CONFLICT (idempotency_key)
        DO NOTHING RETURNING id``

        A returned row means this caller inserted it and owns the posting. No returned row
        means the unique index rejected the insert, so somebody else owns it — and because
        the conflict is decided by the index rather than by a prior SELECT, there is no
        window between the check and the act for a second connection to slip through.

        The claim **is** the insert. It does not write a placeholder for ``LedgerPoster``
        to overwrite: two writes would put the row on disk twice and reintroduce exactly
        the shape this method exists to remove.

        ``fingerprint_matches`` is False when the key was already claimed by a request
        with a different body. ``POST /internal/v1/transactions`` turns that into
        ``409 duplicate_settlement``; an identical body replays 200.
        """
        transaction_id = new_id("txn")
        stmt = (
            pg_insert(LedgerTransaction)
            .values(
                id=transaction_id,
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
                purpose=purpose,
                merchant_id=merchant_id,
                currency=currency,
                livemode=livemode,
                reference_type=reference_type,
                reference_id=reference_id,
                created_by=created_by,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(LedgerTransaction.id)
        )
        inserted = (await session.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            return IdempotencyClaim(transaction_id=inserted, created=True)

        # Lost the race (or replayed our own request). The winner is committed or at
        # least written by a transaction we are now blocked behind, so this read is safe.
        existing = await self.find_by_idempotency_key(session, key)
        if existing is None:  # pragma: no cover - only reachable if the row vanished
            raise TransactionNotFoundError(
                "idempotency key was claimed and then disappeared",
                idempotency_key=key,
            )
        return IdempotencyClaim(
            transaction_id=existing.id,
            created=False,
            fingerprint_matches=existing.request_fingerprint == request_fingerprint,
        )

    async def list_by_reference(
        self,
        session: AsyncSession,
        *,
        reference_type: str,
        reference_id: str,
    ) -> list[LedgerTransaction]:
        """Every transaction pointing at one API-side object, newest first.

        Drives ``GET /internal/v1/transactions?reference_type=&reference_id=``, which is
        what payzeno-api's ``UnsettledChargeSweepJob`` calls to self-heal a charge whose
        ``settlement.completed`` chunk never arrived. Uses
        ``ix_ledger_transaction_reference``.
        """
        stmt = (
            select(LedgerTransaction)
            .where(LedgerTransaction.reference_type == reference_type)
            .where(LedgerTransaction.reference_id == reference_id)
            .order_by(LedgerTransaction.posted_at.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_for_merchant(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        purpose: str | None = None,
        limit: int = 100,
    ) -> list[LedgerTransaction]:
        """One merchant's transactions, newest posted first.

        Uses ``ix_ledger_transaction_merchant_posted``. The optional ``purpose`` filter
        narrows to a single posting kind — the console's payout detail page asks for
        ``payout`` and ``payout_reversal`` separately rather than filtering client-side.
        """
        stmt = select(LedgerTransaction).where(LedgerTransaction.merchant_id == merchant_id)
        if purpose is not None:
            stmt = stmt.where(LedgerTransaction.purpose == purpose)
        stmt = stmt.order_by(LedgerTransaction.posted_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def list_duplicate_idempotency_keys(
        self,
        session: AsyncSession,
        *,
        purpose: str,
        since: datetime,
    ) -> list[DuplicateKeyRow]:
        """Idempotency keys carried by more than one transaction since ``since``.

        Post-``0020`` this returns nothing, because the unique index makes it impossible —
