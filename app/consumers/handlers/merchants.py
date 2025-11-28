"""Handlers for the ``payzeno-merchant-events`` topic.

``merchant_projection`` and ``bank_account_projection`` are the ledger's only view of
data payzeno-api owns. There is no ledger→api HTTP route on the settlement or payout
path, so if these handlers do not run, ``PayoutInitiator.initiate`` holds a bank account
id it cannot resolve and ``SettlementPoster`` has no settlement tolerance to compare a
variance against.

Every write here is a conditional upsert on ``source_occurred_at``. A stale
``merchant.status_changed`` replay that overwrote a newer row would un-suspend a
suspended merchant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from payzeno_contracts.events import (
    MerchantCreatedPayload,
    MerchantStatusChangedPayload,
    MerchantUpdatedPayload,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models.projections import BankAccountProjection, MerchantProjection
from app.ports import Clock
from app.repositories.projections import (
    BankAccountProjectionRepository,
    MerchantProjectionRepository,
)
from app.services.accounts import AccountResolver

logger = get_logger(__name__)


async def handle_merchant_created(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    merchants: MerchantProjectionRepository,
    resolver: AccountResolver,
    clock: Clock,
    event_id: str,
    occurred_at: datetime,
    livemode: bool,
) -> None:
    """Project the merchant and bootstrap its ledger accounts.

    Account bootstrap goes through ``AccountResolver.get_or_create`` — the same writer
    the HTTP bootstrap route and lazy posting-time resolution use. One writer, three
    callers.
    """
    data = _validated(MerchantCreatedPayload, payload)
    projection = _projection_from(data, event_id, occurred_at, livemode, clock.now())
    written = await merchants.upsert_if_newer(session, projection)
    if not written:
        logger.info("merchant_projection_stale", merchant_id=data["merchant_id"])
        return

    await resolver.bootstrap(
        session,
        merchant_id=data["merchant_id"],
        merchant_id=data["merchant_id"],
        risk_tier=data["risk_tier"],
        reserve_bps=int(data["reserve_bps"]),
        payout_delay_days=int(data["payout_delay_days"]),
        payout_schedule=data["payout_schedule"],
        source_occurred_at=occurred_at,
    )
    written = await merchants.upsert_if_newer(session, projection)
    if written and existing.capture_at_settlement != projection.capture_at_settlement:
        logger.warning(
            "capture_at_settlement_changed",
            merchant_id=data["merchant_id"],
            previous=existing.capture_at_settlement,
            current=projection.capture_at_settlement,
            changed_by=data.get("changed_by"),
        )


async def handle_merchant_status_changed(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    merchants: MerchantProjectionRepository,
    clock: Clock,
    event_id: str,
    occurred_at: datetime,
) -> None:
    """Status only. Payouts consult it; settlement does not."""
    data = _validated(MerchantStatusChangedPayload, payload)
    updated = await merchants.update_status_if_newer(
        session,
        merchant_id=data["merchant_id"],
        source_event_id=event_id,
        reason=data.get("reason"),
    )


async def handle_bank_account_verified(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    banks: BankAccountProjectionRepository,
    clock: Clock,
    event_id: str,
    occurred_at: datetime,
    livemode: bool,
) -> None:
    """Project the verified bank account so a payout rail has something to instruct.

    The ledger stores ``account_number_token`` and the last four of whatever identifier
    the scheme uses. It never stores a full account number and never a PAN.
    """
    projection = BankAccountProjection(
        bank_account_id=payload["bank_account_id"],
        currency=payload["currency"],
        routing_last_four=payload.get("routing_last_four"),
        bic=payload.get("bic"),
        source_occurred_at=occurred_at,
    )
    written = await banks.upsert_if_newer(session, projection)
    if not written:
        logger.info(
            "bank_account_projection_stale",
            bank_account_id=payload["bank_account_id"],
        )
        return
    if projection.is_default:
        await banks.clear_other_defaults(
            session,
            merchant_id=projection.merchant_id,
            currency=projection.currency,
            livemode=livemode,
            keep=projection.bank_account_id,
        )
    logger.info(
        "bank_account_projected",
        bank_account_id=projection.bank_account_id,
        default_currency=data["default_currency"],
        status=data["status"],
        risk_tier=data["risk_tier"],
        capture_at_settlement=bool(data.get("capture_at_settlement", False)),
        payout_schedule=data["payout_schedule"],
    return payload
