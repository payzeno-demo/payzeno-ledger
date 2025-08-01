"""``account`` — a node in the double-entry chart of accounts.

**This is not a user account.** The word "account" in the internal API surface always
means this row; a merchant login is a ``User`` in payzeno-api and a payout destination is
a ``BankAccount`` there.

Written only by ``AccountResolver.get_or_create`` (`domain-model.md` §6). It is idempotent
under ``uq_account_merchant_type_currency_livemode``; all three entry points —
``POST /internal/v1/accounts/bootstrap``, the ``merchant.created`` branch of
``MerchantEventConsumer``, and lazy resolution at posting time — delegate to it.
"""

from __future__ import annotations

from typing import ClassVar, Final

from sqlalchemy import Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    TimestampMixin,
    account_status_enum,
    account_type_enum,
    entry_direction_enum,
)

#: The normal balance of every account type. A `debit` account grows on the debit side.
#: `AccountResolver` stamps this onto the row so a trial balance never has to guess, and
#: `LedgerAuditService` compares the stored value against this table nightly.
NORMAL_BALANCE_BY_TYPE: Final[dict[str, str]] = {
    "processor_clearing": "debit",
    "merchant_receivable": "debit",
    "authorization_hold": "credit",
    "merchant_payable": "credit",
    "platform_fee_revenue": "credit",
    "interchange_expense": "debit",
    "scheme_fee_expense": "debit",
    "acquirer_fee_expense": "debit",
    "reserve": "credit",
    "chargeback_liability": "credit",
    "cash": "debit",
    "rounding_adjustment": "credit",
}

#: Account types that exist once per currency at the platform level, with no merchant.
PLATFORM_ACCOUNT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "cash",
        "platform_fee_revenue",
        "interchange_expense",
        "scheme_fee_expense",
        "acquirer_fee_expense",
        "rounding_adjustment",
    }
)

#: Account types bootstrapped for every merchant on `POST /internal/v1/accounts/bootstrap`.
MERCHANT_ACCOUNT_TYPES: Final[tuple[str, ...]] = (
    "merchant_receivable",
    "authorization_hold",
    "merchant_payable",
    "reserve",
    "chargeback_liability",
    "processor_clearing",
)


class Account(Base, TimestampMixin, LivemodeMixin):
    """One (merchant, type, currency, livemode) node of the chart of accounts."""

    __tablename__ = "account"
    entity_name: ClassVar[str] = "account"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: Null for platform-level accounts (cash, revenue, the three expense accounts).
    merchant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(account_type_enum, nullable=False)
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    normal_balance: Mapped[str] = mapped_column(entry_direction_enum, nullable=False)
    status: Mapped[str] = mapped_column(
        account_status_enum, nullable=False, server_default="active"
    )

    __table_args__ = (
        # The idempotency guarantee behind AccountResolver.get_or_create. Widened to
        # include livemode by migration 0032 — before that, a test-mode capture resolved
        # the live merchant_payable account.
        UniqueConstraint(
            "merchant_id",
            "type",
            "currency",
            "livemode",
            name="uq_account_merchant_type_currency_livemode",
        ),
        # Postgres treats NULLs as distinct in a unique index, so the constraint above
        # does not constrain platform accounts at all. This partial unique index is what
        # stops a second `cash` account per currency appearing on a race.
        Index(
            "pix_account_platform",
            "type",
            "currency",
            "livemode",
            unique=True,
            postgresql_where="merchant_id is null",
        ),
        Index("ix_account_merchant_id", "merchant_id"),
    )

    def is_postable(self) -> bool:
        """False for ``frozen`` and ``closed``.

        ``LedgerPoster.post`` checks this after ``AccountResolver.get_or_create`` returns
        and raises :class:`AccountFrozenError` — **409**, not 500. A frozen account is a
        deliberate business state set by ``POST /internal/v1/accounts/{id}/freeze``;
        returning 500 burns the error budget, trips payzeno-api's circuit breaker and
        pages someone for a policy decision.
        """
        return self.status == "active"

    def is_platform(self) -> bool:
        """True for the accounts that have no owning merchant."""
        return self.merchant_id is None

    def expected_normal_balance(self) -> str:
        """What :data:`NORMAL_BALANCE_BY_TYPE` says this account's normal side is."""
        return NORMAL_BALANCE_BY_TYPE[self.type]
