"""`LedgerTransactionRepository` — app/repositories/ledger_transaction.py.

Two methods, and the difference between them is the whole of PAY-2041:

* `find_by_idempotency_key` is a plain SELECT. It reads committed rows. It cannot see a
  concurrent transaction's uncommitted INSERT, so the caller that used it as a guard was
  doing a check-then-act around an irreversible side effect. It is still here — the ops CLI
  and one audit query call it — and it is no longer on the money path.
* `claim_idempotency_key` is `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING`
  in the caller's transaction. Either you inserted the row or somebody else did, and you
  find out inside one statement. That is what "atomic" means and it is what ADR 0011 asks
  for. Added by PR #172.

`.scalars().first()` on the find, not `.one_or_none()`: the 0007 backfill produced genuine
duplicates and `one_or_none()` would have turned an old data artefact into a 500.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.errors import TransactionNotFoundError
from app.models.ledger_transaction import LedgerTransaction
from app.repositories.ledger_transaction import LedgerTransactionRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def repo() -> LedgerTransactionRepository:
    return LedgerTransactionRepository()


async def test_add_and_get(session, repo: LedgerTransactionRepository) -> None:
    await repo.add(session, _txn("txn_lt_1", "settle:sb_A:ri_1"))
    await session.flush()

    assert (await repo.get_or_raise(session, "txn_lt_1")).purpose == "settle"


async def test_get_or_raise_on_a_missing_id(session, repo: LedgerTransactionRepository) -> None:
    with pytest.raises(TransactionNotFoundError):
        await repo.get_or_raise(session, "txn_nope")


async def test_find_by_idempotency_key_cannot_see_an_uncommitted_row(
    sessions, repo: LedgerTransactionRepository
) -> None:
    """The read that used to be the guard, on two connections, at READ COMMITTED.

    This is t3/t4 of the interleaving in the postmortem. Session A inserts and does not
    commit; session B's SELECT returns nothing; both then believe they are the first writer.
    The behaviour asserted here is correct Postgres — the bug was ever relying on it.
    """
    async with sessions.begin() as writer:
        await repo.add(writer, _txn("txn_lt_3", "settle:sb_C:ri_3"))
        await writer.flush()

        async with sessions.begin() as reader:
            assert await repo.find_by_idempotency_key(reader, "settle:sb_C:ri_3") is None


async def test_the_idempotency_key_index_is_unique_since_0020(
    session, repo: LedgerTransactionRepository
) -> None:
    await repo.add(session, _txn("txn_lt_4", "settle:sb_D:ri_4"))
    await session.flush()
    await repo.add(session, _txn("txn_lt_5", "settle:sb_D:ri_4"))

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_list_by_reference(session, repo: LedgerTransactionRepository) -> None:
    await repo.add(session, _txn("txn_lt_8", "settle:sb_H:ri_8", reference_id="ri_8"))
    await repo.add(
        session, _txn("txn_lt_9", "reversal:sb_H:ri_8", purpose="reversal", reference_id="ri_8")
    )
    await session.flush()

    found = await repo.list_by_reference(
        session, reference_type="reconciliation_item", reference_id="ri_8"
    )

    assert {t.purpose for t in found} == {"settle", "reversal"}
