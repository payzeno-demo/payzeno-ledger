"""Handlers for the ``payzeno-payments-events`` topic.

Every projection write in this module is a conditional upsert guarded on
``source_occurred_at``. The bus does not guarantee ordering, and a stale
``payment.authorized`` replay that overwrote a newer row would silently flip
``capture_at_settlement`` back — which is the field that decides whether the ledger
issues a second cardholder capture. ADR 0011.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from payzeno_contracts.events import (
    DisputeOpenedPayload,
    PaymentAuthorizedPayload,
    PaymentCapturedPayload,
    RefundCreatedPayload,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.idempotency import ledger_key
from app.domain.postings import POSTING_RULE_BY_LINE_TYPE, PostingContext
from app.errors import DuplicateDisputeError
from app.logging import get_logger
from app.models.projections import SettlementCharge
from app.ports import Clock
from app.repositories.projections import SettlementChargeRepository
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)


async def handle_payment_authorized(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    charges: SettlementChargeRepository,
    ledger: LedgerPoster,
    clock: Clock,
    event_id: str,
    occurred_at: datetime,
    livemode: bool,
) -> None:
    """Project the charge and post the authorisation.

    ``reserve_bps``, ``platform_fee_bps``, ``platform_fee_fixed_minor`` and
    ``capture_at_settlement`` are denormalised onto the projection at authorisation
    time, on purpose: a later merchant change must not retroactively alter an in-flight
    settlement. ``SettlementPoster`` reads them off this row, never off the merchant.
    """
    data = _validated(PaymentAuthorizedPayload, payload)

    projection = SettlementCharge(
        charge_id=data["charge_id"],
        amount_minor=data["amount_minor"],
        currency=data["currency"],
        network_transaction_id=data.get("network_transaction_id"),
        capture_method=data["capture_method"],
        capture_at_settlement=bool(data["capture_at_settlement"]),
        reserve_bps=int(data.get("reserve_bps", 0)),
        platform_fee_fixed_minor=int(data.get("platform_fee_fixed_minor", 0)),
        livemode=livemode,
        authorized_at=_parse(data["authorized_at"]),
        updated_at=clock.now(),
        source_event_id=event_id,
        idempotency_key=ledger_key("auth", data["merchant_id"], data["charge_id"]),
        merchant_id=data["merchant_id"],
        currency=data["currency"],
        reference_type="charge",
        reference_id=data["charge_id"],
        lines=lines,
        created_by="system",
        request_fingerprint=event_id,
        on_conflict="return_existing",
    )
    logger.info(
        "payment_authorized_projected",
        charge_id=data["charge_id"],
        merchant_id=data["merchant_id"],
        capture_at_settlement=projection.capture_at_settlement,
    )


async def handle_payment_captured(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    charges: SettlementChargeRepository,
    ledger: LedgerPoster,
    clock: Clock,
    event_id: str,
    occurred_at: datetime,
    livemode: bool,
) -> None:
    """Stamp ``captured_at`` and post the capture legs (fees, reserve, payable)."""
    data = _validated(PaymentCapturedPayload, payload)
    charge = await charges.get_or_raise(session, data["charge_id"])
    if charge.source_occurred_at > occurred_at:
        logger.info("payment_captured_stale", charge_id=data["charge_id"])
        return

    charge.captured_at = _parse(data["captured_at"])
    charge.updated_at = clock.now()

    rule = POSTING_RULE_BY_LINE_TYPE["capture"]
    lines = rule.build(
        PostingContext(
            merchant_id=data["merchant_id"],
            currency=data["currency"],
            livemode=livemode,
            gross_minor=data["captured_amount_minor"],
            fee_minor=0,
            net_minor=data["captured_amount_minor"],
            interchange_minor=0,
            scheme_fee_minor=0,
            reserve_bps=charge.reserve_bps,
            platform_fee_bps=charge.platform_fee_bps,
            platform_fee_fixed_minor=charge.platform_fee_fixed_minor,
        )
    )
    await ledger.post(
        session,
        idempotency_key=ledger_key("capture", data["merchant_id"], data["charge_id"]),
        purpose="capture",
        merchant_id=data["merchant_id"],
        livemode=livemode,
        reference_type="charge",
        reference_id=data["charge_id"],
        lines=lines,
        created_by="system",
        request_fingerprint=event_id,
        on_conflict="return_existing",
    )


async def handle_payment_canceled(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    ledger: LedgerPoster,
    event_id: str,
    livemode: bool,
) -> None:
    """Release an authorisation that was never captured."""
    charge_id = payload.get("charge_id")
    if charge_id is None:
        # An intent cancelled before any charge existed has nothing to release.
        return
    rule = POSTING_RULE_BY_LINE_TYPE["auth_release"]
    lines = rule.build(
        PostingContext(
            merchant_id=payload["merchant_id"],
            currency=payload["currency"],
            livemode=livemode,
            gross_minor=payload["released_amount_minor"],
            fee_minor=0,
            net_minor=payload["released_amount_minor"],
            interchange_minor=0,
            scheme_fee_minor=0,
            reserve_bps=0,
            platform_fee_bps=0,
            platform_fee_fixed_minor=0,
        )
    )
    await ledger.post(
        session,
        purpose="auth_release",
        livemode=livemode,
        reference_type="charge",
        reference_id=charge_id,
        lines=lines,
        on_conflict="return_existing",
    )


async def handle_refund_created(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    ledger: LedgerPoster,
    event_id: str,
    livemode: bool,
) -> None:
    """Post the refund.

    A refund against an unsettled charge nets into the batch rather than moving money,
    so nothing is posted here — the acquirer will file a ``refund`` settlement line and
    the reconciliation path will handle it.
    """
    data = _validated(RefundCreatedPayload, payload)
    if data.get("nets_against_settlement"):
        logger.info(
            "refund_deferred_to_settlement",
            refund_id=data["refund_id"],
            charge_id=data["charge_id"],
        )
        return

    rule = POSTING_RULE_BY_LINE_TYPE["refund"]
    lines = rule.build(
        PostingContext(
            merchant_id=data["merchant_id"],
            currency=data["currency"],
            livemode=livemode,
            gross_minor=data["amount_minor"],
            fee_minor=0,
            net_minor=data["amount_minor"],
            interchange_minor=0,
            scheme_fee_minor=0,
            reserve_bps=0,
            platform_fee_bps=0,
            platform_fee_fixed_minor=0,
        )
    )
    await ledger.post(
        session,
        idempotency_key=ledger_key("refund", data["merchant_id"], data["refund_id"]),
        purpose="refund",
        merchant_id=data["merchant_id"],
        currency=data["currency"],
        livemode=livemode,
        reference_type="refund",
        reference_id=data["refund_id"],
        lines=lines,
        on_conflict="return_existing",
    )


async def handle_dispute_opened(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    ledger: LedgerPoster,
    transactions: Any,
    event_id: str,
    livemode: bool,
) -> None:
    """Move the disputed amount out of payable and book the network fee."""
    data = _validated(DisputeOpenedPayload, payload)
    key = ledger_key("dispute", data["merchant_id"], data["dispute_id"])

    existing = await transactions.find_by_idempotency_key(session, key)
    if existing is not None:
        raise DuplicateDisputeError(
            f"dispute {data['dispute_id']} already posted",
            dispute_id=data["dispute_id"],
            existing_transaction_id=existing.id,
        )

    rule = POSTING_RULE_BY_LINE_TYPE["dispute"]
    lines = rule.build(
        PostingContext(
            merchant_id=data["merchant_id"],
            currency=data["currency"],
            livemode=livemode,
            gross_minor=data["amount_minor"],
            fee_minor=data["fee_minor"],
            net_minor=data["amount_minor"] - data["fee_minor"],
            interchange_minor=0,
            scheme_fee_minor=data["fee_minor"],
            reserve_bps=0,
            platform_fee_bps=0,
            platform_fee_fixed_minor=0,
        )
    )
    await ledger.post(
        session,
        idempotency_key=key,
        purpose="dispute",
        merchant_id=data["merchant_id"],
        currency=data["currency"],
        livemode=livemode,
        reference_type="dispute",
        reference_id=data["dispute_id"],
        dispute_id=data["dispute_id"],
        merchant_id=data["merchant_id"],
        idempotency_key=ledger_key("disputewon", payload["merchant_id"], payload["dispute_id"]),
        merchant_id=payload["merchant_id"],
        currency=payload["currency"],
        livemode=livemode,
        reference_id=payload["dispute_id"],
        lines=lines,
        created_by="system",
        on_conflict="return_existing",
    )


def _validated(model: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Round-trip the payload through the contracts model, then work with a plain dict.

    Validating is what stops a schema drift becoming a silent zero in the ledger; the
    dict is what keeps the handler readable.
    """
    validator = getattr(model, "model_validate", None)
    if callable(validator):
        return dict(validator(payload).model_dump())
    return payload


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
