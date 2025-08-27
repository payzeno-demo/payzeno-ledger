"""``settlement_charge`` data access — the ledger's projection of a payzeno-api charge.

The ledger does not own ``charge``; payzeno-api does. What lives here is a read-optimised
copy fed by ``payment.authorized`` and ``payment.captured``, and it exists because
reconciliation has to answer "which charge is this acquirer line" without a synchronous
call into another service on the settlement path.

Four fields are **denormalised at authorisation time on purpose** — ``reserve_bps``,
``platform_fee_bps``, ``platform_fee_fixed_minor`` and ``capture_at_settlement`` — so a
later merchant change cannot retroactively alter an in-flight settlement. That reasoning
matters most for ``capture_at_settlement``: flipping the merchant's flag mid-flight would
otherwise decide whether an already-authorised charge gets a second cardholder capture.
``SettlementPoster`` therefore reads it off **this** row and never off
``merchant_projection``.

Every write is a conditional upsert guarded on ``source_occurred_at`` (migration ``0031``).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ChargeProjectionNotFoundError
from app.models.projections import SettlementCharge
from app.repositories.base import BaseRepository

#: Columns a later event is allowed to overwrite. `charge_id` is the primary key and
#: `source_event_id` / `source_occurred_at` are the guard itself, so they are handled
#: separately below.
_MUTABLE_COLUMNS: tuple[str, ...] = (
    "merchant_id",
    "amount_minor",
    "currency",
    "acquirer",
    "network_transaction_id",
    "processor_reference",
    "capture_method",
    "capture_at_settlement",
    "reserve_bps",
    "platform_fee_bps",
    "platform_fee_fixed_minor",
    "livemode",
    "authorized_at",
    "captured_at",
    "updated_at",
)


class SettlementChargeRepository(BaseRepository[SettlementCharge]):
    """Reads and conditionally upserts the ``settlement_charge`` projection."""

    model: ClassVar[type[SettlementCharge]] = SettlementCharge
    not_found_error: ClassVar[type[ChargeProjectionNotFoundError]] = (
        ChargeProjectionNotFoundError
    )

    def _default_order(self) -> ColumnElement[Any]:
        return SettlementCharge.charge_id

    async def get(  # type: ignore[override]
        self, session: AsyncSession, entity_id: str
    ) -> SettlementCharge | None:
        """Fetch by ``charge_id`` — the primary key is the charge id, not an ``id``."""
        return await session.get(SettlementCharge, entity_id)

    async def upsert_if_newer(
        self, session: AsyncSession, projection: SettlementCharge
    ) -> bool:
        """Write the projection unless a *later* event already wrote this row.

        ``INSERT ... ON CONFLICT (charge_id) DO UPDATE ... WHERE
        settlement_charge.source_occurred_at < EXCLUDED.source_occurred_at``, and the
        boolean is whether the row actually changed.

        The guard is not defensive programming. The bus is at-least-once and unordered,
        and ``source_event_id`` is a ULID of the *event*, not a monotonic token for the
        charge — so without comparing ``source_occurred_at`` a redelivered
        ``payment.authorized`` arriving after ``payment.captured`` silently clears
        ``captured_at``, and a stale event can re-enable ``capture_at_settlement`` in the
        ledger's view of a charge that has already settled.

        Returns False when the incoming event is older. The handler logs and stops; it is
        an ordinary outcome, not an error.
        """
        values = {
            column: getattr(projection, column) for column in _MUTABLE_COLUMNS
        }
        values["charge_id"] = projection.charge_id
        values["source_event_id"] = projection.source_event_id
        values["source_occurred_at"] = projection.source_occurred_at

        stmt = pg_insert(SettlementCharge).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["charge_id"],
            set_={
                **{column: getattr(stmt.excluded, column) for column in _MUTABLE_COLUMNS},
                "source_event_id": stmt.excluded.source_event_id,
                "source_occurred_at": stmt.excluded.source_occurred_at,
            },
            where=SettlementCharge.source_occurred_at < stmt.excluded.source_occurred_at,
        ).returning(SettlementCharge.charge_id)

        written = (await session.execute(stmt)).scalar_one_or_none()
        return written is not None

    async def find_by_processor_reference(
        self, session: AsyncSession, *, acquirer: str, processor_reference: str
    ) -> SettlementCharge | None:
        """Match strategy 1 — the acquirer's own reference for the authorisation.

        ``(acquirer, processor_reference)``, because the two acquirers mint references in
        their own namespaces and ``WF-8831-0042`` is a perfectly plausible Nordpay
        reference as well. Uses ``ix_settlement_charge_processor_reference``, the primary
        reconciliation match key.
        """
        stmt = (
            select(SettlementCharge)
            .where(SettlementCharge.acquirer == acquirer)
            .where(SettlementCharge.processor_reference == processor_reference)
        )
        return (await session.execute(stmt)).scalars().first()

    async def find_by_network_transaction(
        self, session: AsyncSession, *, acquirer: str, network_transaction_id: str | None
    ) -> SettlementCharge | None:
        """Match strategy 2 — the card network's transaction id.

        Unique per acquirer under ``uq_settlement_charge_network_txn``. A null id is not a
        wildcard: it means the acquirer did not give us one, and matching every charge
        that also lacks one would settle an arbitrary charge against this line. Hence the
        early return.
        """
        if network_transaction_id is None:
            return None
        stmt = (
            select(SettlementCharge)
            .where(SettlementCharge.acquirer == acquirer)
            .where(SettlementCharge.network_transaction_id == network_transaction_id)
        )
        return (await session.execute(stmt)).scalars().first()

    async def find_in_amount_window(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        amount_minor: int,
        slack_minor: int = 0,
        authorized_from: dt.datetime | None = None,
        authorized_to: dt.datetime | None = None,
        limit: int = 5,
    ) -> list[SettlementCharge]:
        """Match strategy 3 — same merchant, same currency, near-enough amount, in window.

        Returns a **list** so the caller can count it. The strategy only accepts a match
        when there is exactly one candidate: zero is no evidence and two is ambiguous
        evidence, and both abstain. Even a single hit produces ``needs_review`` and never
        auto-settles, because two charges of the same amount for the same merchant on the
        same day are ordinary and settling the wrong one moves real money.

        ``limit`` is small on purpose — the caller only needs to know "one or more than
        one", and a merchant with four thousand identical £9.99 subscriptions should not
        drag four thousand rows into the session to find that out.
        """
        stmt = (
            select(SettlementCharge)
            .where(SettlementCharge.merchant_id == merchant_id)
            .where(SettlementCharge.currency == currency)
            .where(SettlementCharge.amount_minor >= amount_minor - slack_minor)
            .where(SettlementCharge.amount_minor <= amount_minor + slack_minor)
            .order_by(SettlementCharge.authorized_at.desc())
            .limit(limit)
        )
        if authorized_from is not None:
            stmt = stmt.where(SettlementCharge.authorized_at >= authorized_from)
        if authorized_to is not None:
            stmt = stmt.where(SettlementCharge.authorized_at <= authorized_to)
        return list((await session.execute(stmt)).scalars().all())

    async def list_for_merchant(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        limit: int = 100,
    ) -> list[SettlementCharge]:
        """One merchant's projected charges, newest authorisation first.

        Uses ``ix_settlement_charge_merchant``. Read by the ops CLI and by the audit
        job's cross-database invariant (1) sample.
        """
        stmt = (
            select(SettlementCharge)
            .where(SettlementCharge.merchant_id == merchant_id)
            .order_by(SettlementCharge.authorized_at.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_deferred_capture_charges(
        self,
        session: AsyncSession,
        *,
        merchant_id: str | None = None,
        limit: int = 200,
    ) -> list[SettlementCharge]:
        """Charges whose capture the ledger — not payzeno-api — is holding.

        These are the rows that make this service the component with an irreversible
        cardholder side effect on the settlement path. Eleven travel and lodging merchants
        in production; on the night of PAY-2041 that set was the whole blast radius.
        """
        stmt = (
            select(SettlementCharge)
            .where(SettlementCharge.capture_at_settlement.is_(True))
            .where(SettlementCharge.captured_at.is_(None))
            .order_by(SettlementCharge.authorized_at)
            .limit(limit)
        )
        if merchant_id is not None:
            stmt = stmt.where(SettlementCharge.merchant_id == merchant_id)
        return list((await session.execute(stmt)).scalars().all())
