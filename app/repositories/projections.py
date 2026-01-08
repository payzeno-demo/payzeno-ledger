"""Projection repositories — merchant, bank account, and (re-exported) settlement charge.

Three tables in this service are copies of rows payzeno-api owns. They are eventually
consistent, never authoritative, and every write goes through a conditional upsert guarded
on ``source_occurred_at`` (migration ``0031``).

``SettlementChargeRepository`` lives in its own module because it is big enough to deserve
one and because the matcher imports it directly; it is re-exported here so the consumers
can take all three projections from one import, which is how they were written.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BankAccountProjectionNotFoundError, NotFoundError
from app.models.projections import BankAccountProjection, MerchantProjection
from app.repositories.base import BaseRepository
from app.repositories.settlement_charge import SettlementChargeRepository

__all__ = [
    "BankAccountProjectionRepository",
    "MerchantProjectionRepository",
    "SettlementChargeRepository",
]

_MERCHANT_COLUMNS: tuple[str, ...] = (
    "display_name",
    "country",
    "default_currency",
    "status",
    "risk_tier",
    "reserve_bps",
    "reserve_hold_days",
    "pricing_model",
    "platform_fee_bps",
    "platform_fee_fixed_minor",
    "payout_delay_days",
    "settlement_tolerance_minor",
    "capture_at_settlement",
    "payout_schedule",
    "livemode",
    "updated_at",
)

_BANK_COLUMNS: tuple[str, ...] = (
    "merchant_id",
    "currency",
    "country",
    "scheme",
    "account_number_token",
    "routing_last_four",
    "iban_last_four",
    "bic",
    "sort_code_last_four",
    "status",
    "is_default",
    "livemode",
    "updated_at",
)


class MerchantProjectionRepository(BaseRepository[MerchantProjection]):
    """The ledger's view of a merchant's commercial terms.

    Fed by ``merchant.created`` (which carries every NOT NULL column), ``merchant.updated``
    (the full mutable set — the only way ``capture_at_settlement`` ever becomes true here)
    and ``merchant.status_changed`` (status only).

    ``SettlementPoster`` reads ``settlement_tolerance_minor`` and the fee terms off this
    row. It reads ``capture_at_settlement`` off the **charge**, not off here — see
    `app/repositories/settlement_charge.py`.
    """

    model: ClassVar[type[MerchantProjection]] = MerchantProjection
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    async def update_status_if_newer(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        status: str,
        source_event_id: str,
        source_occurred_at: Any,
        updated_at: Any,
    ) -> bool:
        """Status-only update, for ``merchant.status_changed``.

        A partial event must not blank the columns it does not carry, so this cannot go
        through :meth:`upsert_if_newer` with a half-populated projection — that is how a
        status change would zero a merchant's ``platform_fee_bps``. Same freshness guard,
        narrower ``SET``.
        """
        stmt = (
            MerchantProjection.__table__.update()
            .where(MerchantProjection.merchant_id == merchant_id)
            .where(MerchantProjection.source_occurred_at < source_occurred_at)
            .values(
                status=status,
                updated_at=updated_at,
                source_event_id=source_event_id,
                source_occurred_at=source_occurred_at,
            )
            .returning(MerchantProjection.merchant_id)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def list_capture_at_settlement(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[MerchantProjection]:
        """Merchants whose captures this service is holding.

        Eleven of them in production, all travel or lodging MCCs. The list is short enough
        to print in an incident channel, and on the night of PAY-2041 somebody did.
        """
        stmt = (
            select(MerchantProjection)
            .where(MerchantProjection.capture_at_settlement.is_(True))
            .order_by(MerchantProjection.merchant_id)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


class BankAccountProjectionRepository(BaseRepository[BankAccountProjection]):
    """The ledger's view of a merchant's payout destinations.

    Fed by ``merchant.bank_account_verified``. Without it ``payout.bank_account_id`` is an
    id this service cannot resolve: ``bank_account`` is owned by payzeno-api,
    ``PayoutInitiator.initiate`` has to produce an ACH/SEPA/FPS instruction, and there is
    no ledger→api route on the payout path. This projection is how the account number
    token reaches the rail.

    The projection stores a **vault token** and last-four fragments. It never stores an
    account number and never a PAN (arc PCI).
    """

    async def upsert_if_newer(
        self, session: AsyncSession, projection: BankAccountProjection
    ) -> bool:
        """Apply the projection unless a later event already wrote this row."""
        values = {column: getattr(projection, column) for column in _BANK_COLUMNS}
        values["bank_account_id"] = projection.bank_account_id
        values["source_event_id"] = projection.source_event_id
        values["source_occurred_at"] = projection.source_occurred_at

        stmt = pg_insert(BankAccountProjection).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["bank_account_id"],
            set_={
                **{c: getattr(stmt.excluded, c) for c in _BANK_COLUMNS},
                "source_event_id": stmt.excluded.source_event_id,
                "source_occurred_at": stmt.excluded.source_occurred_at,
            },
            where=BankAccountProjection.source_occurred_at
            < stmt.excluded.source_occurred_at,
        ).returning(BankAccountProjection.bank_account_id)

        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def get_default(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
    ) -> BankAccountProjection:
        """The merchant's default account **for that currency**, or raise.

        Currency is part of the key because there is no FX anywhere in Payzeno: a EUR
        payout requires a EUR account, and paying a EUR balance into a USD account is not
        a rounding problem, it is the wrong bank. ``CreatePayoutRequest.bank_account_id``
        is optional precisely so the ledger can resolve this, and
        ``pix_bank_account_projection_default`` — unique, partial on ``is_default`` — is
        what makes "the default" a single row rather than a coin flip.

        Raises :class:`BankAccountProjectionNotFoundError`, which the payout service lets
        through as a 404 rather than dressing up as a payout failure: the merchant has not
        finished onboarding, and telling them the payout was rejected by their bank would
        be a lie.
        """
        stmt = (
            select(BankAccountProjection)
            .where(BankAccountProjection.merchant_id == merchant_id)
            .where(BankAccountProjection.currency == currency)
            .where(BankAccountProjection.livemode == livemode)
            .where(BankAccountProjection.is_default.is_(True))
        )
        found = (await session.execute(stmt)).scalars().first()
        if found is None:
            raise BankAccountProjectionNotFoundError(
                f"no default {currency} bank account for {merchant_id}",
                merchant_id=merchant_id,
                currency=currency,
            )
        return found

    async def list_for_merchant(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str | None = None,
        default_only: bool = False,
    ) -> list[BankAccountProjection]:
        """Every projected account for a merchant, defaults first."""
        stmt = select(BankAccountProjection).where(
            BankAccountProjection.merchant_id == merchant_id
        )
        if currency is not None:
            stmt = stmt.where(BankAccountProjection.currency == currency)
        if default_only:
            stmt = stmt.where(BankAccountProjection.is_default.is_(True))
        stmt = stmt.order_by(
            BankAccountProjection.is_default.desc(), BankAccountProjection.currency
        )
        return list((await session.execute(stmt)).scalars().all())

    async def clear_other_defaults(
        self,
        session: AsyncSession,
        *,
        merchant_id: str,
        currency: str,
        livemode: bool,
        keep: str,
    ) -> int:
        """Demote every other default for this ``(merchant, currency, livemode)``.

        ``pix_bank_account_projection_default`` is unique, so the promotion of a new
        default has to demote the old one in the same transaction or the insert is
        rejected. payzeno-api emits the two changes as one event; we apply them as one
        statement pair.

        Returns how many rows were demoted — normally one, zero on the first account, and
        anything larger means the unique index was created after the data, which is worth
        seeing in the log.
        """
        stmt = (
            BankAccountProjection.__table__.update()
            .where(BankAccountProjection.merchant_id == merchant_id)
            .where(BankAccountProjection.currency == currency)
            .where(BankAccountProjection.livemode == livemode)
            .where(BankAccountProjection.bank_account_id != keep)
            .where(BankAccountProjection.is_default.is_(True))
            .values(is_default=False)
        )
        return int((await session.execute(stmt)).rowcount or 0)
