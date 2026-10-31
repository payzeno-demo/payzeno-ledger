"""Payouts — moving merchant money out to a bank account.

The advisory-then-row ordering in create_payout is the rule ADR 0011 states: take
acquire_merchant_currency_lock before computing anything, because compute_available
reads the payable balance, and a second request that reads it before the first
commits its payout debit will happily authorise the same money twice.
pix_payout_in_flight is unique as the last word if that is ever bypassed.

**Conversion (RFC platform-v2 §7).** ``req["currency"]`` is the balance being paid out.
When the bank account — the one named, else the merchant's default for that currency,
else the merchant's default account — is in another currency, the payout converts at the
provider mid-rate less ``FX_PAYOUT_MARKUP_BPS``. The lock, the balance read and the
in-flight check are all on the *balance* currency; the payout row's ``amount_minor`` and
``currency`` are what lands in the bank and ``source_*``/``fx_rate`` record where it came
from. The postings are two balanced transactions, one per currency, through
``fx_clearing``; the spread is ``fx_revenue`` in the bank currency.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.locks import AdvisoryLockManager
from app.domain.calendar import BankingCalendar
from app.domain.fx import FxConversion, format_rate
from app.domain.idempotency import ledger_key, sha256_of
from app.domain.ids import new_id
from app.domain.money import SUPPORTED_CURRENCIES, Money
from app.domain.postings import (
    FxPayoutSourcePostingRule,
    FxPayoutTargetPostingRule,
    PostingContext,
    PostingLine,
    mirrored,
)
from app.errors import (
    BankAccountProjectionNotFoundError,
    BankAccountUnusableError,
    InsufficientBalanceError,
    PayoutBlockedError,
    ValidationError,
)
from app.logging import get_logger
from app.models.payout import Payout
from app.models.projections import BankAccountProjection, MerchantProjection
from app.ports import Clock, EventPublisher, FeatureFlags, InitiationResult
from app.repositories.ledger_entry import LedgerEntryRepository
from app.repositories.payout import PayoutRepository
from app.repositories.projections import (
    BankAccountProjectionRepository,
    MerchantProjectionRepository,
)
from app.services.fx import FxRateService, PayoutRate
from app.services.transactions import LedgerPoster

logger = get_logger(__name__)

BLOCKED_MERCHANT_STATUSES = frozenset({"restricted", "suspended", "closed"})
USABLE_BANK_ACCOUNT_STATUSES = frozenset({"verified"})

#: The rail a bank account's scheme implies when the request names none. Every rail
#: refuses a scheme it cannot carry; picking by scheme is what lets a converted payout go
#: to a EUR IBAN over SEPA instead of defaulting to ACH.
DEFAULT_METHOD_BY_SCHEME: dict[str, str] = {
    "aba": "standard_ach",
    "iban": "sepa",
    "uk_sort_code": "faster_payments",
}

_FX_SOURCE_RULE = FxPayoutSourcePostingRule()
_FX_TARGET_RULE = FxPayoutTargetPostingRule()


class PayoutInitiator(abc.ABC):
    """One payout rail."""

    method: ClassVar[str]

    @abc.abstractmethod
    async def initiate(
        self, session: AsyncSession, payout: Payout, bank: BankAccountProjection
    ) -> InitiationResult:
        """Hand the instruction to the rail and return its reference."""

    def _assert_usable(self, bank: BankAccountProjection) -> None:
        if bank.status not in USABLE_BANK_ACCOUNT_STATUSES:
            raise BankAccountUnusableError(
                f"bank account {bank.bank_account_id} is {bank.status}",
                bank_account_id=bank.bank_account_id,
                status=bank.status,
                method=self.method,
            )


@dataclass(frozen=True, slots=True)
class AvailableFunds:
    amount: Money
    posted_minor: int
    in_flight_minor: int


class PayoutCalculator:
    """Works out how much a merchant can actually be paid right now."""

    def __init__(
        self,
        entries: LedgerEntryRepository,
        payouts: PayoutRepository,
        merchants: MerchantProjectionRepository,
    ) -> None:
        self._entries = entries
        self._payouts = payouts
        self._merchants = merchants

    async def compute_available(
        self,
        session: AsyncSession,
        merchant_id: str,
        currency: str,
        cutoff: datetime,
        *,
        livemode: bool = True,
    ) -> Money:
        funds = await self.compute_available_detail(
            session, merchant_id, currency, cutoff, livemode=livemode
        )
        return funds.amount

    async def compute_available_detail(
        self,
        session: AsyncSession,
        merchant_id: str,
        currency: str,
        cutoff: datetime,
        *,
        livemode: bool = True,
    ) -> AvailableFunds:
        """What the merchant can be paid: the ``merchant_payable`` balance.

        Nothing is subtracted from it. Reserve and dispute holds are their own accounts —
        capture credits payable net of the reserve, and a dispute debits payable — and a
        payout debits payable when it is created, so subtracting reserved, disputed or
        in-flight amounts again would count each of them twice. ``in_flight_minor`` is
        reported alongside for the caller, not deducted.
        """
        posted = await self._entries.sum_by_account_and_purpose(
            session,
            merchant_id=merchant_id,
            currency=currency,
            livemode=livemode,
            as_of=cutoff,
        )
        payable = posted.get("merchant_payable", 0)
        in_flight = await self._payouts.sum_in_flight(
            session, merchant_id=merchant_id, currency=currency, livemode=livemode
        )
        available = payable
        return AvailableFunds(
            amount=Money(amount_minor=available, currency=currency),
            posted_minor=payable,
            in_flight_minor=in_flight,
        )


class PayoutService:
    """Creates, cancels and settles payouts."""

    def __init__(
        self,
        locks: AdvisoryLockManager,
        payouts: PayoutRepository,
        merchants: MerchantProjectionRepository,
        banks: BankAccountProjectionRepository,
        calculator: PayoutCalculator,
        calendar: BankingCalendar,
        ledger: LedgerPoster,
        initiators: dict[str, PayoutInitiator],
        publisher: EventPublisher,
        flags: FeatureFlags,
        clock: Clock,
        fx: FxRateService | None = None,
    ) -> None:
        self._locks = locks
        self._payouts = payouts
        self._merchants = merchants
        self._banks = banks
        self._calculator = calculator
        self._calendar = calendar
        self._ledger = ledger
        self._initiators = initiators
        self._publisher = publisher
        self._flags = flags
        self._clock = clock
        #: Optional only so a service built without it (the ops CLI, older tests) still
        #: pays out same-currency; a conversion without it is a ValidationError.
        self._fx = fx

    async def create_payout(
        self, session: AsyncSession, *, merchant_id: str, req: dict
    ) -> Payout:
        currency = str(req.get("currency", "")).upper()
        if not currency:
            raise ValidationError("currency is required", merchant_id=merchant_id)
        if currency not in SUPPORTED_CURRENCIES:
            raise ValidationError(
                f"unsupported currency {currency!r}", merchant_id=merchant_id, currency=currency
            )
        requested_method = req.get("method")
        if requested_method == "same_day_ach" and not self._flags.enabled(
            "payout_same_day_ach", merchant_id=merchant_id
        ):
            raise PayoutBlockedError(
                "same-day ACH is not enabled", merchant_id=merchant_id, method="same_day_ach"
            )

        # Reads that do not touch money come before the lock, and so does the FX rate: it
        # may be an HTTP round trip, and nothing about it depends on the balance.
        merchant = await self._merchants.get_or_raise(session, merchant_id)
        # The request may say which mode it is for; otherwise the merchant's own mode.
        requested_mode = req.get("livemode")
        livemode = merchant.livemode if requested_mode is None else bool(requested_mode)
        if merchant.status in BLOCKED_MERCHANT_STATUSES:
            raise PayoutBlockedError(
                f"merchant is {merchant.status}",
                merchant_id=merchant_id,
                status=merchant.status,
            )
        bank = await self._resolve_bank(
            session,
            merchant=merchant,
            currency=currency,
            livemode=livemode,
            bank_account_id=req.get("bank_account_id"),
        )
        method = str(
            requested_method or DEFAULT_METHOD_BY_SCHEME.get(bank.scheme, "standard_ach")
        )
        initiator = self._initiators.get(method)
        if initiator is None:
            raise ValidationError(f"unsupported payout method {method!r}", method=method)
        rate = await self._rate_for(currency, bank.currency, merchant_id=merchant_id)

        await self._locks.acquire_merchant_currency_lock(session, merchant_id, currency)

        in_flight = await self._payouts.find_in_flight(
            session, merchant_id=merchant_id, currency=currency, livemode=livemode
        )
        if in_flight is not None:
            # pix_payout_in_flight would refuse the insert anyway; this says why.
            raise PayoutBlockedError(
                f"payout {in_flight.id} for this balance is still {in_flight.status}",
                merchant_id=merchant_id,
                currency=currency,
                in_flight_payout_id=in_flight.id,
            )

        cutoff = self._clock.now()
        funds = await self._calculator.compute_available_detail(
            session, merchant_id, currency, cutoff, livemode=livemode
        )
        requested = int(req.get("amount_minor") or funds.amount.amount_minor)
        if funds.amount.amount_minor <= 0 or requested > funds.amount.amount_minor:
            raise InsufficientBalanceError(
                "no payable balance",
                merchant_id=merchant_id,
                currency=currency,
                available_minor=funds.amount.amount_minor,
                requested_minor=requested,
            )
        if requested <= 0:
            raise ValidationError(
                "amount_minor must be positive", merchant_id=merchant_id, amount_minor=requested
            )

        source = Money(amount_minor=requested, currency=currency)
        conversion = FxRateService.convert(source, rate) if rate is not None else None
        landed = conversion.target if conversion is not None else source

        available_on = self._calendar.next_business_day(
            cutoff.date(), landed.currency, method
        )
        payout = Payout(
            id=new_id("po"),
            merchant_id=merchant_id,
            bank_account_id=bank.bank_account_id,
            amount_minor=landed.amount_minor,
            currency=landed.currency,
            status="scheduled",
            method=method,
            available_on=available_on,
            statement_descriptor=str(req.get("statement_descriptor") or "PAYZENO PAYOUT")[:22],
            livemode=livemode,
            # Stamped from the clock rather than left to the server default: the route
            # serialises the row inside the same transaction, and an expired server
            # default is an implicit lazy load an AsyncSession refuses.
            created_at=cutoff,
            updated_at=cutoff,
        )
        if conversion is not None:
            payout.source_amount_minor = conversion.source.amount_minor
            payout.source_currency = conversion.source.currency
            payout.fx_rate = conversion.rate
            payout.fx_spread_minor = conversion.spread_minor
        await self._payouts.add(session, payout)

        if conversion is None:
            payout.ledger_transaction_id = await self._post_payout(session, payout)
        else:
            payout.ledger_transaction_id = await self._post_converted_payout(
                session, payout, conversion
            )

        result = await initiator.initiate(session, payout, bank)
        payout.arrival_estimate = result.arrival_estimate
        payout.bank_reference = result.rail_reference
        payout.initiated_at = result.submitted_at

        await self._publisher.publish(
            "payout.scheduled",
            {
                "payout_id": payout.id,
                "merchant_id": merchant_id,
                "bank_account_id": bank.bank_account_id,
                "amount_minor": payout.amount_minor,
                "currency": payout.currency,
                "method": method,
                "available_on": available_on.isoformat(),
                "scheduled_at": self._clock.now().isoformat(),
            },
            merchant_id=merchant_id,
            correlation_id=payout.id,
            session=session,
            livemode=livemode,
        )
        logger.info(
            "payout_created",
            payout_id=payout.id,
            merchant_id=merchant_id,
            amount_minor=payout.amount_minor,
            currency=payout.currency,
            source_amount_minor=payout.source_amount_minor,
            source_currency=payout.source_currency,
            fx_rate=format_rate(payout.fx_rate) if payout.fx_rate is not None else None,
            method=method,
        )
        return payout

    async def _resolve_bank(
        self,
        session: AsyncSession,
        *,
        merchant: MerchantProjection,
        currency: str,
        livemode: bool,
        bank_account_id: object,
    ) -> BankAccountProjection:
        """The account named, else the default for this currency, else the merchant's default.

        "The merchant's default" when the balance's currency has no account of its own is
        the default in ``merchant.default_currency``, and failing that the first default
        the merchant has in any currency. Either of those means a conversion.
        """
        merchant_id = merchant.merchant_id
        if bank_account_id:
            bank = await self._banks.get_or_raise(session, str(bank_account_id))
            if bank.merchant_id != merchant_id:
                # A bank account id is not a capability. Paying one merchant's balance to
                # another's account is the worst thing this service could do on request.
                raise ValidationError(
                    "bank account does not belong to this merchant",
                    merchant_id=merchant_id,
                    bank_account_id=bank.bank_account_id,
                )
            return bank

        same_currency = await self._banks.find_default(
            session, merchant_id=merchant_id, currency=currency, livemode=livemode
        )
        if same_currency is not None:
            return same_currency

        defaults = await self._banks.list_defaults(
            session, merchant_id=merchant_id, livemode=livemode
        )
        if defaults:
            preferred = merchant.default_currency
            return min(defaults, key=lambda b: (b.currency != preferred, b.currency))
        raise BankAccountProjectionNotFoundError(
            f"no default bank account for {merchant_id}",
            merchant_id=merchant_id,
            currency=currency,
        )

    async def _rate_for(
        self, from_currency: str, to_currency: str, *, merchant_id: str
    ) -> PayoutRate | None:
        if from_currency == to_currency:
            return None
        if self._fx is None:
            raise ValidationError(
                "this payout needs a currency conversion and no FX provider is configured",
                merchant_id=merchant_id,
                currency=from_currency,
                bank_currency=to_currency,
            )
        return await self._fx.payout_rate(from_currency, to_currency)

    async def _post_payout(self, session: AsyncSession, payout: Payout) -> str:
        """Same currency: Dr ``merchant_payable`` / Cr ``cash``."""
        posted = await self._ledger.post(
            session,
            idempotency_key=ledger_key("payout", payout.merchant_id, payout.id),
            purpose="payout",
            merchant_id=payout.merchant_id,
            currency=payout.currency,
            livemode=payout.livemode,
            reference_type="payout",
            reference_id=payout.id,
            lines=[
                PostingLine(
                    account_type="merchant_payable",
                    direction="debit",
                    amount_minor=payout.amount_minor,
                ),
                PostingLine(
                    account_type="cash", direction="credit", amount_minor=payout.amount_minor
                ),
            ],
            created_by="system",
            request_fingerprint=sha256_of(ledger_key("payoutfp", payout.merchant_id, payout.id)),
        )
        return posted.transaction.id

    async def _post_converted_payout(
        self, session: AsyncSession, payout: Payout, conversion: FxConversion
    ) -> str:
        """Two transactions, one per currency, each balanced through ``fx_clearing``.

        Returns the source-currency one — the transaction that moved the merchant's
        balance, which is what ``payout.ledger_transaction_id`` has always pointed at. The
        bank-currency one is found by ``reference_type='payout'`` and its own key.
        """
        source_lines = _fx_source_lines(payout, conversion.source.amount_minor)
        target_lines = _fx_target_lines(
            payout,
            mid_minor=conversion.mid_target_minor,
            payout_minor=conversion.target.amount_minor,
            spread_minor=conversion.spread_minor,
        )
        source = await self._ledger.post(
            session,
            idempotency_key=ledger_key("payout", payout.merchant_id, payout.id),
            purpose="payout",
            merchant_id=payout.merchant_id,
            currency=conversion.source.currency,
            livemode=payout.livemode,
            reference_type="payout",
            reference_id=payout.id,
            lines=source_lines,
            created_by="system",
            request_fingerprint=sha256_of(ledger_key("payoutfp", payout.merchant_id, payout.id)),
        )
        await self._ledger.post(
            session,
            idempotency_key=ledger_key("payoutfx", payout.merchant_id, payout.id),
            purpose="payout",
            merchant_id=payout.merchant_id,
            currency=conversion.target.currency,
            livemode=payout.livemode,
            reference_type="payout",
            reference_id=payout.id,
            lines=target_lines,
            created_by="system",
            request_fingerprint=sha256_of(
                ledger_key("payoutfxfp", payout.id, format_rate(conversion.rate))
            ),
        )
        return source.transaction.id

    async def cancel_payout(self, session: AsyncSession, payout_id: str) -> Payout:
        payout = await self._payouts.get_or_raise(session, payout_id)
        if payout.status not in ("scheduled",):
            raise PayoutBlockedError(
                f"payout {payout_id} is {payout.status} and cannot be cancelled",
                payout_id=payout_id,
                status=payout.status,
            )
        payout.status = "canceled"
        reversal = await self._reverse_payout_posting(session, payout, reason="canceled")
        payout.reversal_transaction_id = reversal
        logger.info("payout_canceled", payout_id=payout_id)
        return payout

    async def mark_paid(
        self, session: AsyncSession, payout_id: str, *, paid_at: datetime, bank_reference: str
    ) -> Payout:
        await self._locks.acquire_item_lock(session, payout_id)
        payout = await self._payouts.get_or_raise(session, payout_id)
        if payout.status == "paid":
            return payout
        payout.status = "paid"
        payout.paid_at = paid_at
        payout.bank_reference = bank_reference
        await self._publisher.publish(
            "payout.paid",
            {
                "payout_id": payout.id,
                "merchant_id": payout.merchant_id,
                "amount_minor": payout.amount_minor,
                "currency": payout.currency,
                "ledger_transaction_id": payout.ledger_transaction_id,
                "paid_at": paid_at.isoformat(),
            },
            merchant_id=payout.merchant_id,
            correlation_id=payout.id,
            session=session,
        )
        logger.info("payout_paid", payout_id=payout_id, bank_reference=bank_reference)
        return payout

    async def mark_failed(
        self,
        session: AsyncSession,
        payout_id: str,
        *,
        failure_code: str,
        failure_message: str,
    ) -> Payout:
        await self._locks.acquire_item_lock(session, payout_id)
        payout = await self._payouts.get_or_raise(session, payout_id)
        if payout.status in ("failed", "returned"):
            return payout

        # A rail rejection and a bank return are the same reversal and a different
        # conversation. If we already told the merchant the money was on its way, it
        # left, and what came back is a return — status `returned`, event
        # `payout.returned`, and the console shows it against the original payout
        # rather than as a fresh failure.
        if payout.status in ("paid", "in_transit"):
            return await self._mark_returned(
                session,
                payout,
                failure_code=failure_code,
                failure_message=failure_message,
            )

        # Reversal first, status second. The reversal's own queries autoflush the payout
        # row, and a row already marked `failed` with no reversal id yet is exactly what
        # chk_payout_reversal_present refuses — every rail failure used to 500 here.
        reversal = await self._reverse_payout_posting(session, payout, reason=failure_code)
        payout.status = "failed"
        payout.failure_code = failure_code
        payout.failure_message = failure_message
        payout.failed_at = self._clock.now()
        payout.reversal_transaction_id = reversal
        await self._publisher.publish(
            "payout.failed",
            {
                "payout_id": payout.id,
                "merchant_id": payout.merchant_id,
                "bank_account_id": payout.bank_account_id,
                "amount_minor": payout.amount_minor,
                "currency": payout.currency,
                "failure_code": failure_code,
                "failure_message": failure_message,
                "reversal_transaction_id": reversal,
                "requires_merchant_action": failure_code in ("account_closed", "invalid_details"),
                "retry_scheduled_for": None,
                "failed_at": payout.failed_at.isoformat(),
            },
            merchant_id=payout.merchant_id,
            correlation_id=payout.id,
            session=session,
        )
        logger.warning(
            "payout_failed",
            payout_id=payout_id,
            failure_code=failure_code,
            reversal_transaction_id=reversal,
        )
        return payout

    async def _mark_returned(
        self,
        session: AsyncSession,
        payout: Payout,
        *,
        failure_code: str,
        failure_message: str,
    ) -> Payout:
        """An ACH/SEPA return: the money left, the receiving bank sent it back.

        The reversal is identical to the failure path — the merchant's payable balance
        has to come back either way — but the return can arrive five business days after
        `paid`, against a balance that has since funded another payout. That is why the
        reversal is posted here and not left to the next sweep.
        """
        reversal = await self._reverse_payout_posting(session, payout, reason=failure_code)
        returned = await self._payouts.mark_returned(
            session,
            payout.id,
            failure_code=failure_code,
            failure_message=failure_message,
            reversal_transaction_id=reversal,
            at=self._clock.now(),
        )
        await self._publisher.publish(
            "payout.returned",
            {
                "payout_id": returned.id,
                "merchant_id": returned.merchant_id,
                "bank_account_id": returned.bank_account_id,
                "amount_minor": returned.amount_minor,
                "currency": returned.currency,
                "failure_code": failure_code,
                "failure_message": failure_message,
                "reversal_transaction_id": reversal,
                "bank_reference": returned.bank_reference,
                "returned_at": returned.returned_at.isoformat(),
            },
            merchant_id=returned.merchant_id,
            correlation_id=returned.id,
            session=session,
            livemode=returned.livemode,
        )
        logger.warning(
            "payout_returned",
            payout_id=returned.id,
            failure_code=failure_code,
            reversal_transaction_id=reversal,
        )
        return returned

    async def _reverse_payout_posting(
        self, session: AsyncSession, payout: Payout, *, reason: str
    ) -> str:
        if payout.is_converted():
            return await self._reverse_converted_payout(session, payout, reason=reason)
        posted = await self._ledger.post(
            session,
            idempotency_key=ledger_key("payoutrev", payout.merchant_id, payout.id),
            purpose="payout_reversal",
            merchant_id=payout.merchant_id,
            currency=payout.currency,
            livemode=payout.livemode,
            reference_type="payout",
            reference_id=payout.id,
            lines=[
                PostingLine(
                    account_type="cash", direction="debit", amount_minor=payout.amount_minor
                ),
                PostingLine(
                    account_type="merchant_payable",
                    direction="credit",
                    amount_minor=payout.amount_minor,
                ),
            ],
            created_by="system",
            request_fingerprint=sha256_of(ledger_key("payoutrevfp", payout.id, reason)),
        )
        return posted.transaction.id

    async def _reverse_converted_payout(
        self, session: AsyncSession, payout: Payout, *, reason: str
    ) -> str:
        """Mirror both legs of a converted payout, at the rate it converted at.

        The merchant gets back the full source amount in the source currency, the spread
        is un-booked, and ``cash`` in the bank currency is restored — no second conversion,
        so no FX gain or loss is recognised on a failure or a return. Returns the
        source-currency reversal, which is what ``reversal_transaction_id`` records and
        ``chk_payout_reversal_present`` requires.
        """
        source_minor = payout.balance_amount_minor
        source_currency = payout.balance_currency
        spread_minor = payout.fx_spread_minor or 0
        target_lines = mirrored(
            _fx_target_lines(
                payout,
                mid_minor=payout.amount_minor + spread_minor,
                payout_minor=payout.amount_minor,
                spread_minor=spread_minor,
            )
        )
        await self._ledger.post(
            session,
            idempotency_key=ledger_key("payoutfxrev", payout.merchant_id, payout.id),
            purpose="payout_reversal",
            merchant_id=payout.merchant_id,
            currency=payout.currency,
            livemode=payout.livemode,
            reference_type="payout",
            reference_id=payout.id,
            lines=target_lines,
            created_by="system",
            request_fingerprint=sha256_of(ledger_key("payoutfxrevfp", payout.id, reason)),
        )
        posted = await self._ledger.post(
            session,
            idempotency_key=ledger_key("payoutrev", payout.merchant_id, payout.id),
            purpose="payout_reversal",
            merchant_id=payout.merchant_id,
            currency=source_currency,
            livemode=payout.livemode,
            reference_type="payout",
            reference_id=payout.id,
            lines=mirrored(_fx_source_lines(payout, source_minor)),
            created_by="system",
            request_fingerprint=sha256_of(ledger_key("payoutrevfp", payout.id, reason)),
        )
        return posted.transaction.id

    async def due_payouts(self, session: AsyncSession, *, on: date) -> list[Payout]:
        return await self._payouts.list_due(session, on=on)


def _fx_context(
    payout: Payout, currency: str, *, gross: int, net: int, fee: int
) -> PostingContext:
    return PostingContext(
        merchant_id=payout.merchant_id,
        currency=currency,
        livemode=payout.livemode,
        gross_minor=gross,
        fee_minor=fee,
        net_minor=net,
        interchange_minor=0,
        scheme_fee_minor=0,
        reserve_bps=0,
        platform_fee_bps=0,
        platform_fee_fixed_minor=0,
    )


def _fx_source_lines(payout: Payout, source_minor: int) -> list[PostingLine]:
    """Dr ``merchant_payable`` / Cr ``fx_clearing``, in the balance currency."""
    ctx = _fx_context(
        payout, payout.balance_currency, gross=source_minor, net=source_minor, fee=0
    )
    return _FX_SOURCE_RULE.build(ctx)


def _fx_target_lines(
    payout: Payout, *, mid_minor: int, payout_minor: int, spread_minor: int
) -> list[PostingLine]:
    """Dr ``fx_clearing`` / Cr ``cash`` + ``fx_revenue``, in the bank currency."""
    ctx = _fx_context(
        payout, payout.currency, gross=mid_minor, net=payout_minor, fee=spread_minor
    )
    return _FX_TARGET_RULE.build(ctx)
