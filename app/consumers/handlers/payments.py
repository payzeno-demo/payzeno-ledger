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
        currency=data["currency"],
        capture_method=data["capture_method"],
        updated_at=clock.now(),
        lines=lines,
        merchant_id=data["merchant_id"],
        purpose="capture",
        merchant_id=data["merchant_id"],
        reference_type="charge",
        reference_type="charge",
        lines=lines,
        idempotency_key=ledger_key("refund", data["merchant_id"], data["refund_id"]),
        purpose="refund",
        currency=data["currency"],
        reference_id=data["refund_id"],
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
        purpose="dispute",
        merchant_id=data["merchant_id"],
        reference_type="dispute",
        livemode=livemode,
        lines=lines,
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
