"""`app/api/deps.py` — the per-route dependency surface.

There are two layers of internal auth and they are not redundant.
`InternalAuthMiddleware` (§1.6) is the belt: it rejects anything without mTLS, the
`X-Payzeno-Service` header and `INTERNAL_API_SECRET`, and it owns the exemption list for
`/healthz`, `/readyz` and `/metrics`. `require_internal_service` is the braces: a
per-route FastAPI dependency, so the auth requirement is visible on the handler rather
than being an invisible property of the app factory.

The braces matter more than they look. Routers are mounted in `create_app`, and a router
added without the middleware — which is exactly what happens the first time someone writes
a test app — would otherwise be wide open.

A note on how the handler tests in this directory call things: they invoke the handler
function directly with positional arguments, in declaration order, rather than going
through the ASGI stack. That means a `TESTS` edge to the handler by name rather than to
`TestClient`, and it keeps the service doubles honest.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.deps import (
    require_internal_service,
    require_staff_claim,
)
from app.errors import DualControlRequiredError, ValidationError

pytestmark = pytest.mark.asyncio


class FakeRequest:
    """The attribute surface `require_internal_service` reads off `Request`."""

    def __init__(self, headers: dict[str, str] | None = None, path: str = "/internal/v1/payouts") -> None:
        self.headers = headers or {}
        self.url = type("URL", (), {"path": path})()
        self.scope: dict[str, Any] = {"path": path}
        self.state = type("State", (), {})()


class Settings:
    internal_api_secret = "dev-internal-secret-do-not-use-anywhere-real"


def _valid_headers() -> dict[str, str]:
    return {
        "x-payzeno-service": "payzeno-api",
        "x-payzeno-internal-secret": Settings.internal_api_secret,
    }


async def test_a_valid_internal_caller_is_accepted() -> None:
    request = FakeRequest(_valid_headers())

    principal = await require_internal_service(request, Settings())

    assert principal.service == "payzeno-api"


async def test_a_missing_service_header_is_rejected() -> None:
    headers = _valid_headers()
    del headers["x-payzeno-service"]

    with pytest.raises(ValidationError):
        await require_internal_service(FakeRequest(headers), Settings())


async def test_a_missing_secret_is_rejected() -> None:
    headers = _valid_headers()
    del headers["x-payzeno-internal-secret"]

    with pytest.raises(ValidationError):
        await require_internal_service(FakeRequest(headers), Settings())


async def test_a_wrong_secret_is_rejected() -> None:
    """The three services share one secret. All three must match or nothing talks."""
    headers = _valid_headers()
    headers["x-payzeno-internal-secret"] = "hunter2"

    with pytest.raises(ValidationError):
        await require_internal_service(FakeRequest(headers), Settings())


async def test_an_unknown_service_name_is_rejected() -> None:
    """Only payzeno-api and payzeno-billing-legacy call the ledger.

    Anything else presenting a valid secret is a service that has been copy-pasted into
    existence, and the review conversation should happen before the traffic does.
    """
    headers = _valid_headers()
    headers["x-payzeno-service"] = "payzeno-console"

    with pytest.raises(ValidationError):
        await require_internal_service(FakeRequest(headers), Settings())


@pytest.mark.parametrize("service", ["payzeno-api", "payzeno-billing-legacy"])
async def test_both_real_callers_are_accepted(service: str) -> None:
    headers = _valid_headers()
    headers["x-payzeno-service"] = service

    principal = await require_internal_service(FakeRequest(headers), Settings())

    assert principal.service == service


async def test_the_staff_claim_is_required_on_ops_routes() -> None:
    """`/internal/v1/ops/*` is internal **plus** a staff claim.

    Internal auth says "another Payzeno service is calling". It does not say "a named
    human authorised this", and adjustments move merchant money.
    """
    headers = _valid_headers()
    principal = await require_internal_service(FakeRequest(headers), Settings())

    with pytest.raises(DualControlRequiredError):
        await require_staff_claim(principal)


async def test_a_staff_claim_passes_through_the_actor() -> None:
    headers = _valid_headers()
    headers["x-payzeno-staff-user"] = "usr_staff_1"
    principal = await require_internal_service(FakeRequest(headers), Settings())

    staff = await require_staff_claim(principal)

    assert staff.staff_user_id == "usr_staff_1"
