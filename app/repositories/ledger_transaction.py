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
