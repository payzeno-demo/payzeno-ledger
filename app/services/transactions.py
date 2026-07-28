"""The one writer of ``ledger_transaction`` and ``ledger_entry``.

Nothing else in payzeno-ledger INSERTs either table. Every posting — settlement,
capture, refund, dispute, payout, reserve, adjustment, reversal — arrives here as a
list of :class:`~app.domain.postings.PostingLine` and is checked against the double-entry
invariants in ``domain-model.md`` §7 before a row is written.

Invariants enforced, in order:

1. at least two lines
2. sum(debits) == sum(credits)
3. every line shares the transaction currency
4. every amount is strictly positive (direction carries the sign, never the amount)
5. every line shares the transaction's ``livemode``

The merchant balance cache is maintained **in this transaction**, not by a follower, so
it can never lag a committed posting. See ``data-model.md`` §3.10.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ids import new_id
from app.domain.postings import PostingLine
from app.errors import (
    AccountFrozenError,
    CurrencyMismatchError,
    DuplicateSettlementError,
    LivemodeMismatchError,
    NegativeAmountError,
    UnbalancedTransactionError,
)
from app.logging import get_logger
from app.models.ledger_entry import LedgerEntry
from app.models.ledger_transaction import LedgerTransaction
from app.ports import Clock
from app.publishers.outbox import OutboxPublisher
from app.repositories.account import AccountRepository
from app.repositories.balance_cache import MerchantBalanceCacheRepository
from app.repositories.ledger_entry import LedgerEntryRepository
from app.repositories.ledger_transaction import IdempotencyClaim, LedgerTransactionRepository
from app.services.accounts import AccountResolver

logger = get_logger(__name__)

#: Account types whose balance movement feeds `merchant_balance_cache.available_minor`.
AVAILABLE_ACCOUNT_TYPES: frozenset[str] = frozenset({"merchant_payable"})
PENDING_ACCOUNT_TYPES: frozenset[str] = frozenset({"merchant_pending"})
RESERVED_ACCOUNT_TYPES: frozenset[str] = frozenset({"merchant_reserve"})
DISPUTED_ACCOUNT_TYPES: frozenset[str] = frozenset({"merchant_disputed"})


@dataclass(frozen=True, slots=True)
class PostResult:
    """What :meth:`LedgerPoster.post` returns.

    ``created`` is False only when ``on_conflict='return_existing'`` and the key was
    already present.
    """

    transaction: LedgerTransaction
    created: bool


# `IdempotencyClaim` is DEFINED in app/repositories/ledger_transaction.py, next to the
# `claim_idempotency_key` statement that produces it, and re-exported here because
# `interfaces.md` §3.5 documents it at this path and callers import it from both. One
# class, two names to reach it — a second dataclass with the same three fields is how the
# repository's `fingerprint_matches` quietly stops reaching `LedgerPoster`.
__all__ = ["IdempotencyClaim", "LedgerPoster", "PostResult"]


class LedgerPoster:
    """Writes one balanced transaction and its entries.

    Stateless and session-per-call: one instance is constructed in ``app/container.py``
    and shared by the settlement poster, the payout service, both consumers and the
    adjustment service, all of which run concurrently on different connections.
    """

    def __init__(
        self,
        transactions: LedgerTransactionRepository,
        entries: LedgerEntryRepository,
        accounts: AccountRepository,
        balances: MerchantBalanceCacheRepository,
        resolver: AccountResolver,
        publisher: OutboxPublisher,
        clock: Clock,
    ) -> None:
        self._transactions = transactions
        self._entries = entries
        self._accounts = accounts
        self._balances = balances
        self._resolver = resolver
        self._publisher = publisher
        self._clock = clock

    async def post(
        self,
        session: AsyncSession,
        *,
        idempotency_key: str,
        purpose: str,
        merchant_id: str | None,
        currency: str,
        livemode: bool,
        reference_type: str,
        reference_id: str,
        lines: list[PostingLine],
        created_by: str,
        request_fingerprint: str,
        on_conflict: Literal["raise", "return_existing"] = "raise",
    ) -> PostResult:
        self._assert_invariants(lines, currency=currency, livemode=livemode)

        # The claim IS the insert — one statement,
        #   INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id
        # decided by uq_ledger_transaction_idempotency_key rather than by a prior SELECT.
        #
        # This replaced a `find_by_idempotency_key` read followed by an `add`. Those were
        # two statements at READ COMMITTED, and between them any other connection could
        # run the same read and get the same answer: on the night of PAY-2041 two of them
        # did, 1,847 times. The index was non-unique until `0020`, so nothing downstream
        # objected either. Do not reintroduce a read here "for a nicer error message" —
        # the error message is what `fingerprint_matches` is for.
        claim = await self._transactions.claim_idempotency_key(
            session,
            key=idempotency_key,
            purpose=purpose,
            merchant_id=merchant_id,
            currency=currency,
            reference_type=reference_type,
            reference_id=reference_id,
            created_by=created_by,
            livemode=livemode,
            request_fingerprint=request_fingerprint,
        )

        if not claim.created:
            # Somebody else owns the key. Nothing has been written by us and — critically
            # for the caller — no entries and no external call have happened.
            existing = await self._transactions.get_or_raise(session, claim.transaction_id)
            if on_conflict == "return_existing":
                return PostResult(transaction=existing, created=False)
            raise DuplicateSettlementError(
                f"idempotency_key {idempotency_key} already posted",
                idempotency_key=idempotency_key,
                existing_transaction_id=claim.transaction_id,
                fingerprint_matches=claim.fingerprint_matches,
            )

        transaction = await self._transactions.get_or_raise(session, claim.transaction_id)

        entries = await self._materialise_entries(
            session,
            transaction=transaction,
            lines=lines,
            merchant_id=merchant_id,
            currency=currency,
            livemode=livemode,
        )
        await self._entries.add_all(session, entries)
        await self._apply_balance_delta(
            session,
            merchant_id=merchant_id,
            currency=currency,
            livemode=livemode,
            entries=entries,
            transaction_id=transaction.id,
        )

        await self._publish_posted(session, transaction, entries)

        logger.info(
            "ledger_transaction_posted",
            transaction_id=transaction.id,
            purpose=purpose,
            merchant_id=merchant_id,
            currency=currency,
            entry_count=len(entries),
        )
        return PostResult(transaction=transaction, created=True)

    async def _publish_posted(
        self,
        session: AsyncSession,
        transaction: LedgerTransaction,
        entries: list[LedgerEntry],
    ) -> None:
        """Stage ``ledger.transaction_posted`` in the posting transaction.

        Nobody subscribes to this one — it is the ops/analytics firehose feed and the
        postmortem timeline, and it is deliberately unconsumed. It goes through the
        outbox anyway: a rolled-back posting that had emitted an audit event would be
        worse than no audit event at all.
        """
        await self._publisher.publish(
            "ledger.transaction_posted",
            {
                "transaction_id": transaction.id,
                "idempotency_key": transaction.idempotency_key,
                "purpose": transaction.purpose,
                "merchant_id": transaction.merchant_id,
                "currency": transaction.currency,
                "reference_type": transaction.reference_type,
                "reference_id": transaction.reference_id,
                "created_by": transaction.created_by,
                "entry_count": len(entries),
                "amount_minor": sum(
                    entry.amount_minor for entry in entries if entry.direction == "debit"
                ),
                "posted_at": transaction.posted_at.isoformat(),
            },
            merchant_id=transaction.merchant_id,
            correlation_id=transaction.id,
            session=session,
            livemode=transaction.livemode,
        )

    async def reverse(
        self,
        session: AsyncSession,
        *,
        original: LedgerTransaction,
        reason: str,
        idempotency_key: str,
        created_by: str,
    ) -> PostResult:
        """Post the mirror image of an existing transaction.

        ``ledger_entry`` is append-only (trigger ``trg_ledger_entry_immutable``), so a
        correction is always a new transaction with every direction flipped, never an
        UPDATE or DELETE of the original.
        """
        original_entries = await self._entries.list_for_transaction(session, original.id)
        flipped = [
            PostingLine(
                account_type=entry.account_type,
                direction="credit" if entry.direction == "debit" else "debit",
                amount_minor=entry.amount_minor,
            )
            for entry in original_entries
        ]
        result = await self.post(
            session,
            idempotency_key=idempotency_key,
            purpose="reversal",
            merchant_id=original.merchant_id,
            currency=original.currency,
            livemode=original.livemode,
            reference_type="ledger_transaction",
            reference_id=original.id,
            lines=flipped,
            created_by=created_by,
            request_fingerprint=original.request_fingerprint,
        )
        result.transaction.reverses_transaction_id = original.id
        logger.info(
            "ledger_transaction_reversed",
            transaction_id=result.transaction.id,
            reverses=original.id,
            reason=reason,
        )
        return result

    async def _materialise_entries(
        self,
        session: AsyncSession,
        *,
        transaction: LedgerTransaction,
        lines: list[PostingLine],
        merchant_id: str | None,
        currency: str,
        livemode: bool,
    ) -> list[LedgerEntry]:
        entries: list[LedgerEntry] = []
        for sequence, line in enumerate(lines, start=1):
            account = await self._resolver.get_or_create(
                session,
                merchant_id=None if _is_platform_account(line.account_type) else merchant_id,
                type_=line.account_type,
                currency=currency,
                livemode=livemode,
            )
            if account.status in ("frozen", "closed"):
                raise AccountFrozenError(
                    f"account {account.id} is {account.status}",
                    account_id=account.id,
                    merchant_id=merchant_id,
                    account_type=line.account_type,
                )
            entries.append(
                LedgerEntry(
                    id=new_id("le"),
                    transaction_id=transaction.id,
                    account_id=account.id,
                    account_type=line.account_type,
                    direction=line.direction,
                    amount_minor=line.amount_minor,
                    currency=currency,
                    livemode=livemode,
                    sequence=sequence,
                )
            )
        return entries

    async def _apply_balance_delta(
        self,
        session: AsyncSession,
        *,
        merchant_id: str | None,
        currency: str,
        livemode: bool,
        entries: list[LedgerEntry],
        transaction_id: str,
    ) -> None:
        if merchant_id is None:
            return
        available = _signed_total(entries, AVAILABLE_ACCOUNT_TYPES)
        pending = _signed_total(entries, PENDING_ACCOUNT_TYPES)
        reserved = _signed_total(entries, RESERVED_ACCOUNT_TYPES)
        disputed = _signed_total(entries, DISPUTED_ACCOUNT_TYPES)
        if not any((available, pending, reserved, disputed)):
            return
        await self._balances.apply_delta(
            session,
            merchant_id=merchant_id,
            currency=currency,
            livemode=livemode,
            available_delta=available,
            pending_delta=pending,
            reserved_delta=reserved,
            disputed_delta=disputed,
            last_transaction_id=transaction_id,
            computed_at=self._clock.now(),
        )

    @staticmethod
    def _assert_invariants(
        lines: list[PostingLine], *, currency: str, livemode: bool
    ) -> None:
        if len(lines) < 2:
            raise UnbalancedTransactionError(
                "a transaction needs at least two entries",
                line_count=len(lines),
            )

        debits = sum(line.amount_minor for line in lines if line.direction == "debit")
        credits = sum(line.amount_minor for line in lines if line.direction == "credit")
        if debits != credits:
            raise UnbalancedTransactionError(
                "debits and credits do not balance",
                debit_minor=debits,
                credit_minor=credits,
            )

        for line in lines:
            if line.amount_minor <= 0:
                raise NegativeAmountError(
                    "posting amounts are unsigned; direction carries the sign",
                    account_type=line.account_type,
                    amount_minor=line.amount_minor,
                )
            line_currency = getattr(line, "currency", currency)
            if line_currency != currency:
                raise CurrencyMismatchError(
                    "posting line currency differs from the transaction currency",
                    expected=currency,
                    actual=line_currency,
                )
            line_livemode = getattr(line, "livemode", livemode)
            if line_livemode != livemode:
                raise LivemodeMismatchError(
                    "posting line livemode differs from the transaction livemode",
                    expected=livemode,
                    actual=line_livemode,
                )


def _signed_total(entries: list[LedgerEntry], account_types: frozenset[str]) -> int:
    """Credits increase what Payzeno owes the merchant; debits decrease it."""
    total = 0
    for entry in entries:
        if entry.account_type not in account_types:
            continue
        total += entry.amount_minor if entry.direction == "credit" else -entry.amount_minor
    return total


def _is_platform_account(account_type: str) -> bool:
    return account_type.startswith("platform_") or account_type in ("cash", "acquirer_receivable")
