"""Row builders. Real ORM instances with sane defaults, every field overridable.

The rule these follow: **a factory never invents a value a test cares about.** Every
argument a test passes is the thing that test is about, and everything it does not pass is
a default that is realistic, boring, and identical across the suite. That is why
``make_item`` defaults to ``status='pending'`` and ``variance_minor=0`` and why the
tolerance on ``make_merchant_projection`` is 100 minor units: a test that asserts on
variance sets the variance, and a test that does not should never trip the tolerance
branch by accident.

They return unattached ORM objects. The service layer's in-memory repositories
(``tests/services/conftest.py``) hold them in dictionaries; the integration layer passes
them to a real repository's ``add``/``upsert_if_newer`` against real Postgres. Same
objects, both ways — a factory that returned dicts for one and models for the other is a
factory nobody trusts.

Ids are readable and prefixed rather than ULIDs. ``ri_integ_0`` in a failing assertion
tells you which fixture built it; ``01JQ8Z4M...`` tells you nothing at 3am.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.models.account import Account
from app.models.projections import (
    BankAccountProjection,
    MerchantProjection,
    SettlementCharge,
)
from app.models.reconciliation_item import ReconciliationItem
from app.models.settlement_batch import SettlementBatch

__all__ = [
    "DEFAULT_NOW",
    "make_account_set",
    "make_bank_account_projection",
    "make_batch",
    "make_charge_projection",
    "make_item",
    "make_merchant_projection",
]

#: The suite's default "now". Deliberately the night of PAY-2041: when an integration test
#: fails at 3am and the row it prints is dated 2026-01-22T00:15Z, the postmortem and the
#: reconciliation runbook are one grep away.
DEFAULT_NOW = dt.datetime(2026, 1, 22, 0, 15, tzinfo=dt.timezone.utc)


def make_batch(
    *,
    batch_id: str = "sb_default",
    acquirer: str = "worldflow",
    currency: str = "USD",
    processing_date: dt.date | None = None,
    file_reference: str | None = None,
    status: str = "open",
    expected_total_minor: int = 0,
    posted_total_minor: int = 0,
    item_count: int = 0,
    **overrides: Any,
) -> SettlementBatch:
    """One ``settlement_batch``.

    ``status`` defaults to ``open`` because that is what the importer creates; anything
    reconciliation-shaped has to say ``closed`` explicitly, which keeps
    "why did this batch reconcile" answerable from the test body alone.
    """
    return SettlementBatch(
        id=batch_id,
        acquirer=acquirer,
        currency=currency,
        processing_date=processing_date or DEFAULT_NOW.date(),
        file_reference=file_reference or f"WF-{batch_id}",
        expected_total_minor=expected_total_minor,
        posted_total_minor=posted_total_minor,
        funded_amount_minor=0,
        funding_event_id=None,
        item_count=item_count,
        status=status,
        opened_at=DEFAULT_NOW,
        closed_at=None,
        reconciled_at=None,
        funded_at=None,
        livemode=True,
        **overrides,
    )


def make_item(
    *,
    item_id: str = "ri_default",
    batch_id: str = "sb_default",
    charge_id: str | None = "ch_default",
    merchant_id: str | None = "mer_default",
    line_type: str = "sale",
    currency: str = "USD",
    gross_minor: int = 10_000,
    fee_minor: int = 290,
    net_minor: int = 9_710,
    interchange_minor: int = 0,
    scheme_fee_minor: int = 0,
    expected_gross_minor: int | None = None,
    variance_minor: int = 0,
    acquirer: str = "worldflow",
    acquirer_reference: str | None = None,
    network_reference: str | None = None,
    match_method: str = "exact_reference",
    status: str = "pending",
    attempt_count: int = 0,
    next_attempt_at: dt.datetime | None = None,
    settled_transaction_id: str | None = None,
    **overrides: Any,
) -> ReconciliationItem:
    """One ``reconciliation_item``.

    ``charge_id`` is nullable and defaults to a real value: an *unmatched* item is a
    distinct condition (``OrphanedItemError``) and a test that wants one passes
    ``charge_id=None`` on purpose.
    """
    return ReconciliationItem(
        id=item_id,
        batch_id=batch_id,
        charge_id=charge_id,
        merchant_id=merchant_id,
        line_type=line_type,
        currency=currency,
        gross_minor=gross_minor,
        fee_minor=fee_minor,
        net_minor=net_minor,
        interchange_minor=interchange_minor,
        scheme_fee_minor=scheme_fee_minor,
        expected_gross_minor=(
            gross_minor if expected_gross_minor is None else expected_gross_minor
        ),
        variance_minor=variance_minor,
        acquirer=acquirer,
        acquirer_reference=acquirer_reference or f"WF-{item_id}",
        network_reference=network_reference,
        match_method=match_method,
        matched_at=DEFAULT_NOW,
        status=status,
        attempt_count=attempt_count,
        last_error_code=None,
        last_attempt_at=None,
        next_attempt_at=next_attempt_at or DEFAULT_NOW,
        settled_transaction_id=settled_transaction_id,
        livemode=True,
        **overrides,
    )


def make_merchant_projection(
    *,
    merchant_id: str = "mer_default",
    display_name: str | None = None,
    status: str = "active",
    risk_tier: str = "standard",
    reserve_bps: int = 0,
    reserve_hold_days: int = 0,
    pricing_model: str = "blended",
    platform_fee_bps: int = 290,
    platform_fee_fixed_minor: int = 30,
    payout_delay_days: int = 2,
    settlement_tolerance_minor: int = 100,
    capture_at_settlement: bool = False,
    payout_schedule: str = "daily",
    source_occurred_at: dt.datetime | None = None,
    **overrides: Any,
) -> MerchantProjection:
    """One ``merchant_projection``.

    ``source_occurred_at`` is a real argument rather than "now" because every write to
    this table is a conditional upsert guarded on it, and half the projection tests are
    about a stale event arriving after a fresh one.
    """
    occurred = source_occurred_at or DEFAULT_NOW
    return MerchantProjection(
        merchant_id=merchant_id,
        display_name=display_name or merchant_id.replace("mer_", "").title(),
        country="US",
        default_currency="USD",
        status=status,
        risk_tier=risk_tier,
        reserve_bps=reserve_bps,
        reserve_hold_days=reserve_hold_days,
        pricing_model=pricing_model,
        platform_fee_bps=platform_fee_bps,
        platform_fee_fixed_minor=platform_fee_fixed_minor,
        payout_delay_days=payout_delay_days,
        settlement_tolerance_minor=settlement_tolerance_minor,
        capture_at_settlement=capture_at_settlement,
        payout_schedule=payout_schedule,
        source_event_id=f"evt_{merchant_id}",
        source_occurred_at=occurred,
        updated_at=occurred,
        livemode=True,
        **overrides,
    )


def make_charge_projection(
    *,
    charge_id: str = "ch_default",
    merchant_id: str = "mer_default",
    amount_minor: int = 10_000,
    currency: str = "USD",
    acquirer: str = "worldflow",
    network_transaction_id: str | None = None,
    processor_reference: str | None = None,
    capture_method: str = "automatic",
    capture_at_settlement: bool = False,
    reserve_bps: int = 0,
    platform_fee_bps: int = 290,
    platform_fee_fixed_minor: int = 30,
    source_occurred_at: dt.datetime | None = None,
    **overrides: Any,
) -> SettlementCharge:
    """One ``settlement_charge`` projection.

    ``capture_at_settlement`` lives here and **not** on the merchant for a reason
    ``SettlementPoster`` depends on: the flag is denormalised onto the charge at
    authorisation, so a merchant flipping it afterwards cannot decide whether an in-flight
    charge gets a second cardholder capture (PAY-1652).
    """
    occurred = source_occurred_at or DEFAULT_NOW
    return SettlementCharge(
        charge_id=charge_id,
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency=currency,
        acquirer=acquirer,
        network_transaction_id=network_transaction_id,
        processor_reference=processor_reference,
        capture_method=capture_method,
        capture_at_settlement=capture_at_settlement,
        reserve_bps=reserve_bps,
        platform_fee_bps=platform_fee_bps,
        platform_fee_fixed_minor=platform_fee_fixed_minor,
        authorized_at=occurred,
        captured_at=None,
        source_event_id=f"evt_{charge_id}",
        source_occurred_at=occurred,
        updated_at=occurred,
        livemode=True,
        **overrides,
    )


def make_bank_account_projection(
    *,
    bank_account_id: str = "ba_default",
    merchant_id: str = "mer_default",
    currency: str = "USD",
    country: str = "US",
    scheme: str = "ach",
    status: str = "verified",
    is_default: bool = True,
    source_occurred_at: dt.datetime | None = None,
    **overrides: Any,
) -> BankAccountProjection:
    """One ``bank_account_projection``.

    ``account_number_token`` is a vault token and ``*_last_four`` is four digits. There is
    no field here that could hold a full account number, which is the point — the ledger
    is out of PCI scope by construction and the factory should not be the one place that
    quietly puts it back in.
    """
    occurred = source_occurred_at or DEFAULT_NOW
    return BankAccountProjection(
        bank_account_id=bank_account_id,
        merchant_id=merchant_id,
        currency=currency,
        country=country,
        scheme=scheme,
        account_number_token=f"tok_{bank_account_id}",
        routing_last_four="0021",
        iban_last_four=None,
        bic=None,
        sort_code_last_four=None,
        status=status,
        is_default=is_default,
        source_event_id=f"evt_{bank_account_id}",
        source_occurred_at=occurred,
        updated_at=occurred,
        livemode=True,
        **overrides,
    )


def make_account_set(
    *,
    merchant_id: str = "mer_default",
    currency: str = "USD",
    livemode: bool = True,
) -> list[Account]:
    """The chart-of-accounts rows one merchant needs before anything can post.

    Returned as a set rather than one at a time because a balanced transaction always
    touches at least two of them, and a test that seeds one account and then fails an
    invariant is testing the fixture. Normal balances follow ``domain-model.md`` §6:
    assets and expenses are debit-normal, liabilities and revenue are credit-normal.
    """
    suffix = "live" if livemode else "test"
    spec: list[tuple[str, str, str | None]] = [
        ("merchant_balance", "credit", merchant_id),
        ("merchant_reserve", "credit", merchant_id),
        ("acquirer_receivable", "debit", None),
        ("platform_revenue", "credit", None),
        ("processing_expense", "debit", None),
        ("payouts_payable", "credit", merchant_id),
    ]
    return [
        Account(
            id=f"acc_{account_type}_{merchant_id if owner else 'platform'}_{currency}_{suffix}",
            merchant_id=owner,
            type=account_type,
            currency=currency,
            normal_balance=normal,
            status="active",
            livemode=livemode,
        )
        for account_type, normal, owner in spec
    ]
