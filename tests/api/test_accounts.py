"""`app/api/routers/accounts.py` — api-surface.md §10.1.

Three routes, one caller: payzeno-api, from merchant activation and from the risk
console. The balance reads that share this group live in `test_balances.py`, because they
have a second caller (payzeno-billing-legacy) and a different reason to be careful.

`bootstrap_accounts` delegates to `AccountResolver.get_or_create`, the single writer of
`account`. The route does not INSERT anything itself and it must not start: three
mechanisms for creating accounts is three ways to violate
`uq_account_merchant_type_currency_livemode`.

Every handler here returns a **dict**, not an ORM row — `_serialise` runs inside the
session, before it closes. That ordering is not incidental: returning the row and letting
FastAPI's `response_model` read it would touch a detached instance after the transaction
ended, which fails as a lazy-load error at render time rather than as anything useful.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.api.routers.accounts import bootstrap_accounts, freeze_account, list_accounts
from app.errors import AccountFrozenError, AccountNotFoundError

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 16, 14, 0, tzinfo=UTC)

#: What `require_internal_service` resolved to. It ends up on the audit log line and, when
#: a retry body carries no `requested_by`, in the attribution.
CALLER = "payzeno-api"


def _account(
    account_type: str, *, status: str = "active", created_at: datetime | None = None
) -> Any:
    return type(
        "Account",
        (),
        {
            "id": f"acct_{account_type}",
            "merchant_id": "mer_api",
            "type": account_type,
            "currency": "USD",
            "livemode": True,
            "status": status,
            "created_at": created_at or NOW,
        },
    )()


class StubResolver:
    def __init__(self, *, frozen: bool = False) -> None:
        self.bootstrapped: list[tuple[str, str, bool]] = []
        self.frozen = frozen
        self.listed: list[dict[str, Any]] = []
        self.froze: list[tuple[str, str]] = []

    async def bootstrap(
        self, session: Any, *, merchant_id: str, currency: str, livemode: bool
    ) -> list[Any]:
        self.bootstrapped.append((merchant_id, currency, livemode))
        return [
            _account("merchant_payable"),
            _account("reserve"),
            _account("chargeback_liability"),
        ]

    async def list_accounts(
        self,
        session: Any,
        *,
        merchant_id: str | None,
        currency: str | None,
        type_: str | None,
        livemode: bool,
    ) -> list[Any]:
        self.listed.append(
            {
                "merchant_id": merchant_id,
                "currency": currency,
                "type_": type_,
                "livemode": livemode,
            }
        )
        return [_account("merchant_payable"), _account("reserve")]

    async def freeze(self, session: Any, account_id: str, *, reason: str) -> Any:
        if account_id == "acct_ghost":
            raise AccountNotFoundError(entity_id=account_id)
        if self.frozen:
            raise AccountFrozenError(
                f"account {account_id} is already frozen", account_id=account_id
            )
        self.froze.append((account_id, reason))
        return _account("merchant_payable", status="frozen")


class Body:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


# --------------------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------------------


async def test_bootstrap_delegates_to_the_resolver(sessions_factory) -> None:
    resolver = StubResolver()

    response = await bootstrap_accounts(
        Body(merchant_id="mer_api", currency="USD", livemode=True),
        sessions_factory,
        resolver,
        CALLER,
    )

    assert resolver.bootstrapped == [("mer_api", "USD", True)]
    assert len(response["accounts"]) == 3


async def test_bootstrap_is_idempotent_for_the_caller(sessions_factory) -> None:
    """payzeno-api calls this on every merchant activation and retries on timeout.

    `get_or_create` under the unique key means a repeat returns the same set rather than
    a 409, which is what makes the caller's retry safe.
    """
    resolver = StubResolver()
    body = Body(merchant_id="mer_api", currency="USD", livemode=True)

    first = await bootstrap_accounts(body, sessions_factory, resolver, CALLER)
    second = await bootstrap_accounts(body, sessions_factory, resolver, CALLER)

    assert [a["id"] for a in first["accounts"]] == [a["id"] for a in second["accounts"]]


async def test_list_accounts_passes_every_filter(sessions_factory) -> None:
    resolver = StubResolver()

    response = await list_accounts(
        sessions_factory, resolver, "mer_api", "USD", "merchant_payable", True
    )

    assert resolver.listed == [
        {
            "merchant_id": "mer_api",
            "currency": "USD",
            "type_": "merchant_payable",
            "livemode": True,
        }
    ]
    assert len(response["data"]) == 2


async def test_freeze_returns_the_frozen_account(sessions_factory) -> None:
    resolver = StubResolver()

    account = await freeze_account(
        Body(reason="risk_review"),
        sessions_factory,
        resolver,
        CALLER,
        "acct_merchant_payable",
    )

    assert account["status"] == "frozen"
    assert resolver.froze == [("acct_merchant_payable", "risk_review")]


async def test_freezing_an_unknown_account_is_a_404(sessions_factory) -> None:
    resolver = StubResolver()

    with pytest.raises(AccountNotFoundError) as excinfo:
        await freeze_account(
            Body(reason="risk"), sessions_factory, resolver, CALLER, "acct_ghost"
        )

    assert excinfo.value.http_status == 404


async def test_freezing_a_frozen_account_is_409_not_500(sessions_factory) -> None:
    """`account_frozen` moved from 500 to 409 deliberately.

    A frozen account is a business state somebody chose. A 500 burns the error budget,
    trips payzeno-api's circuit breaker and pages a human about a policy decision.
    """
    resolver = StubResolver(frozen=True)

    with pytest.raises(AccountFrozenError) as excinfo:
        await freeze_account(
            Body(reason="again"),
            sessions_factory,
            resolver,
            CALLER,
            "acct_merchant_payable",
        )

    assert excinfo.value.http_status == 409
    assert excinfo.value.code == "account_frozen"


async def test_a_pre_existing_set_reports_zero_created(sessions_factory) -> None:
    """`created` is derived from `created_at`, not returned by the resolver.

    `get_or_create` upserts and has no reason to care which branch it took, so the route
    counts the accounts whose `created_at` post-dates the start of the request. Rows made
    on an earlier call are older than that instant and count for nothing — which is what
    makes payzeno-api's retry observably a no-op rather than observably a second create.
    """
    resolver = StubResolver()

    response = await bootstrap_accounts(
        Body(merchant_id="mer_api", currency="USD", livemode=True),
        sessions_factory,
        resolver,
        CALLER,
    )

    assert response["created"] == 0
    assert len(response["accounts"]) == 3


async def test_freshly_created_accounts_are_counted(sessions_factory) -> None:
    class FreshResolver(StubResolver):
        async def bootstrap(
            self, session: Any, *, merchant_id: str, currency: str, livemode: bool
        ) -> list[Any]:
            self.bootstrapped.append((merchant_id, currency, livemode))
            future = datetime.now(UTC) + timedelta(seconds=5)
            return [
                _account("merchant_payable", created_at=future),
                _account("reserve", created_at=future),
                _account("chargeback_liability", created_at=NOW),
            ]

    response = await bootstrap_accounts(
        Body(merchant_id="mer_fresh", currency="USD", livemode=True),
        sessions_factory,
        FreshResolver(),
        CALLER,
    )

    assert response["created"] == 2


async def test_bootstrap_runs_in_one_transaction(sessions_factory) -> None:
    """Three accounts, one unique key, one transaction. A partial set is unusable."""
    await bootstrap_accounts(
        Body(merchant_id="mer_api", currency="USD", livemode=True),
        sessions_factory,
        StubResolver(),
        CALLER,
    )

    assert sessions_factory.begin_count == 1
    assert sessions_factory.last.committed is True


async def test_listing_with_no_merchant_selects_the_platform_accounts(
    sessions_factory,
) -> None:
    """`merchant_id=None` is `pix_account_platform` — what the trial balance asks for.

    It is a filter that means something rather than a filter that was forgotten, which is
    why the resolver is handed the `None` explicitly instead of the route dropping the
    key.
    """
    resolver = StubResolver()

    await list_accounts(sessions_factory, resolver, None, "USD", None, True)

    assert resolver.listed == [
        {"merchant_id": None, "currency": "USD", "type_": None, "livemode": True}
    ]


async def test_the_account_list_is_unpaginated(sessions_factory) -> None:
    """A merchant has at most a dozen accounts per currency. A cursor here is ceremony."""
    response = await list_accounts(sessions_factory, StubResolver(), "mer_api", None, None, True)

    assert set(response) == {"data"}
