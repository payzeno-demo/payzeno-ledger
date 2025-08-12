"""The OpenAPI snapshot — `contracts/ledger-openapi.json`.

This file is one half of a cross-repo contract test. payzeno-api's
`test/contract/ledger-routes.contract.spec.ts` reads a checked-in copy of this exact
snapshot and asserts that every path its `LedgerHttpClient` calls exists in it. So a route
renamed here fails a test in a repository this one has never heard of, which is the point:
the alternative is finding out in staging.

Regenerate with `make openapi`. If this test fails and the change is intentional, the
regenerated snapshot goes in the same PR as the route change, and payzeno-api's copy goes
in a PR that merges after it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.main import create_app

pytestmark = pytest.mark.asyncio

SNAPSHOT = Path(__file__).resolve().parents[2] / "contracts" / "ledger-openapi.json"

#: Every path in api-surface.md §10, verbatim. The list is the contract.
EXPECTED_PATHS = {
    "/internal/v1/accounts/bootstrap",
    "/internal/v1/accounts",
    "/internal/v1/accounts/{account_id}/freeze",
    "/internal/v1/balances/{merchant_id}",
    "/internal/v1/balances/{merchant_id}/history",
    "/internal/v1/transactions",
    "/internal/v1/transactions/{transaction_id}",
    "/internal/v1/transactions/{transaction_id}/reverse",
    "/internal/v1/entries",
    "/internal/v1/settlement-batches",
    "/internal/v1/settlement-batches/{batch_id}",
    "/internal/v1/settlement-batches/{batch_id}/close",
    "/internal/v1/settlement-batches/{batch_id}/items",
    "/internal/v1/settlement-batches/{batch_id}/funding",
    "/internal/v1/settlement-imports",
    "/internal/v1/reconciliation/runs",
    "/internal/v1/reconciliation/runs/{run_id}",
    "/internal/v1/reconciliation/items/{item_id}/retry",
    "/internal/v1/reconciliation/backlog",
    "/internal/v1/payouts",
    "/internal/v1/payouts/{payout_id}",
    "/internal/v1/payouts/{payout_id}/cancel",
    "/internal/v1/payouts/{payout_id}/mark-paid",
    "/internal/v1/payouts/{payout_id}/mark-failed",
    "/internal/v1/invoices/lines/stage",
    "/internal/v1/invoices/lines",
    "/internal/v1/ops/audit/trial-balance",
    "/internal/v1/ops/adjustments",
    "/internal/v1/ops/adjustments/{request_id}/approve",
    "/internal/v1/ops/items/{item_id}/match",
    "/healthz",
    "/readyz",
}

#: Served, in the snapshot, and NOT in api-surface.md §10.
#:
#: `POST /internal/v1/transactions/bulk` went in during month 2 for the migration
#: backfill and has had no caller since month 5. It never made it into the public
#: contract and it has never been deleted either — the last attempt (PAY-1904) stalled
#: on "who is going to confirm the Java side isn't hitting it", and nobody did.
#: Listing it here rather than deleting the route is the honest version of the same
#: stalemate: the test stays green and the route stays visible.
GRANDFATHERED = {
    "/internal/v1/transactions/bulk",
}


@pytest.fixture(scope="module")
def generated() -> dict:
    return create_app().openapi()


@pytest.fixture(scope="module")
def snapshot() -> dict:
    with SNAPSHOT.open(encoding="utf-8") as handle:
        return json.load(handle)


async def test_the_snapshot_exists() -> None:
    """payzeno-api's contract test reads this file. It is not optional."""
    assert SNAPSHOT.exists(), f"missing {SNAPSHOT}; run `make openapi`"


async def test_every_contracted_path_is_served(generated: dict) -> None:
    served = set(generated["paths"])

    missing = EXPECTED_PATHS - served
    assert not missing, f"routes in api-surface.md §10 that are not mounted: {sorted(missing)}"


async def test_no_undocumented_paths_are_served(generated: dict) -> None:
    """A route that exists and is not in the contract is a route nobody reviewed.

    `/metrics` is mounted by `prometheus_client`'s ASGI app rather than by a router, so
    it does not appear here.
    """
    served = {path for path in generated["paths"] if not path.startswith("/docs")}

    extra = served - EXPECTED_PATHS - {"/openapi.json", "/metrics"} - GRANDFATHERED
    assert not extra, f"undocumented routes: {sorted(extra)}"


async def test_the_snapshot_matches_the_generated_spec(generated: dict, snapshot: dict) -> None:
    """The actual gate. Regenerate with `make openapi` when this fails on purpose."""
    assert set(generated["paths"]) == set(snapshot["paths"])


async def test_the_retry_route_documents_its_409(generated: dict) -> None:
    """`409 settlement_locked` is a normal outcome, not an error, and it is contracted.

    payzeno-api translates it into `LedgerRejectedException` carrying the code verbatim,
    and the console's `useRetrySettlementItem()` branches on that code to show "already
    settling" rather than a failure toast. If the 409 disappears from the spec, the
    console shows an error for the single most common outcome of that button.
    """
    responses = generated["paths"]["/internal/v1/reconciliation/items/{item_id}/retry"]["post"][
        "responses"
    ]

    assert "409" in responses
    assert "202" in responses


async def test_the_health_routes_carry_no_security_requirement(generated: dict) -> None:
    """They are exempt from `InternalAuthMiddleware` and the spec has to say so.

    An ECS health check cannot present mTLS plus two headers, and payzeno-api's `/readyz`
    pings ours from a plain client.
    """
    for path in ("/healthz", "/readyz"):
        operation = generated["paths"][path]["get"]
        assert not operation.get("security")


async def test_every_other_route_declares_internal_auth(generated: dict) -> None:
    for path, operations in generated["paths"].items():
        if path in {"/healthz", "/readyz", "/metrics"}:
            continue
        for method, operation in operations.items():
            if method not in {"get", "post", "patch", "delete"}:
                continue
            assert operation.get("security"), f"{method.upper()} {path} has no security"
