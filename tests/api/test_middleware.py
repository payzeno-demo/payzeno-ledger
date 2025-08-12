"""The middleware chain — app/middleware/, registered in app/main.py::create_app.

Six middleware, in a contracted order, and the order is the test. Starlette applies
middleware outermost-first in registration order, so:

1. `RequestIdMiddleware`      mints/echoes `X-Request-Id` and seeds a contextvar
2. `CorrelationIdMiddleware`  reads `X-Correlation-Id`, falls back to the request id
3. `StructuredAccessLogMiddleware`  one line per request
4. `PrometheusMiddleware`     the histograms behind `GET /metrics`
5. `RedactionMiddleware`      scrubs bodies before they reach the access log
6. `InternalAuthMiddleware`   mTLS + `X-Payzeno-Service` + `INTERNAL_API_SECRET`

Two of those positions are load-bearing rather than aesthetic. Correlation id must be
seeded before anything logs or publishes, because every publisher reads that contextvar to
stamp `EventEnvelope.correlation_id` — a trace that stops at the ledger boundary is a
trace nobody can follow through a settlement. And redaction must run before the access
log, or the log has already written the thing redaction exists to remove.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.main import create_app
from app.middleware.access_log import StructuredAccessLogMiddleware
from app.middleware.correlation_id import (
    CorrelationIdMiddleware,
    current_correlation_id,
)
from app.middleware.internal_auth import PUBLIC_PATHS, InternalAuthMiddleware
from app.middleware.metrics import PrometheusMiddleware
from app.middleware.redaction import (
    REDACTED,
    RedactionMiddleware,
    redact_headers,
    redact_text,
)
from app.middleware.request_id import RequestIdMiddleware, current_request_id

pytestmark = pytest.mark.asyncio

EXPECTED_ORDER = [
    RequestIdMiddleware,
    CorrelationIdMiddleware,
    StructuredAccessLogMiddleware,
    PrometheusMiddleware,
    RedactionMiddleware,
    InternalAuthMiddleware,
]


def _installed(app: Any) -> list[type]:
    return [entry.cls for entry in app.user_middleware]


async def test_the_chain_is_registered_in_the_contracted_order() -> None:
    app = create_app()

    assert _installed(app) == EXPECTED_ORDER


async def test_correlation_id_is_seeded_before_anything_logs() -> None:
    """Everything downstream reads the contextvar it sets.

    Publishers stamp `EventEnvelope.correlation_id` from it. Seeding it after the access
    log means every event published during that request carries an empty correlation and
    the trace dies at the ledger boundary.
    """
    installed = _installed(create_app())

    assert installed.index(CorrelationIdMiddleware) < installed.index(
        StructuredAccessLogMiddleware
    )


async def test_redaction_runs_before_the_access_log() -> None:
    """Otherwise the log has already written what redaction exists to remove."""
    installed = _installed(create_app())

    assert installed.index(RedactionMiddleware) < installed.index(InternalAuthMiddleware)


async def test_request_id_is_minted_when_the_caller_does_not_send_one() -> None:
    middleware = RequestIdMiddleware(app=_echo_app)
    scope = _http_scope(headers=[])

    response = await _call(middleware, scope)

    assert response["request_id"]
    assert response["request_id"].startswith("req_")


async def test_request_id_is_echoed_when_the_caller_does_send_one() -> None:
    """payzeno-api mints it at the edge. The ledger must not overwrite it."""
    middleware = RequestIdMiddleware(app=_echo_app)
    scope = _http_scope(headers=[(b"x-request-id", b"req_from_api")])

    response = await _call(middleware, scope)

    assert response["request_id"] == "req_from_api"


async def test_correlation_id_falls_back_to_the_request_id() -> None:
    """A request that starts at the ledger is still traceable."""
    middleware = CorrelationIdMiddleware(app=_echo_app)
    scope = _http_scope(headers=[(b"x-request-id", b"req_alone")])

    response = await _call(middleware, scope)

    assert response["correlation_id"] == "req_alone"


async def test_correlation_id_is_preserved_across_the_boundary() -> None:
    middleware = CorrelationIdMiddleware(app=_echo_app)
    scope = _http_scope(
        headers=[(b"x-request-id", b"req_1"), (b"x-correlation-id", b"corr_from_console")]
    )

    response = await _call(middleware, scope)

    assert response["correlation_id"] == "corr_from_console"


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
async def test_the_three_ops_endpoints_are_exempt_from_internal_auth(path: str) -> None:
    """An ECS health check cannot present mTLS plus two headers.

    Neither can a Prometheus scraper. They are bound to the internal network and are not
    routed by the ingress, which is the actual control.
    """
    assert path in PUBLIC_PATHS


async def test_every_other_path_is_not_exempt() -> None:
    assert "/internal/v1/payouts" not in PUBLIC_PATHS
    assert "/internal/v1/transactions" not in PUBLIC_PATHS


async def test_redaction_masks_a_pan_shaped_string() -> None:
    """arc PCI, `FLAG_REDACT_PAN_IN_LOGS`, non-removable.

    The ledger should never see a PAN — it stores `account_number_token` and a last four —
    but "should never" is not a control, and this is the control.

    The mask keeps the BIN and the last four. That is deliberate: a fully starred line is
    useless when support is matching a cardholder complaint to a charge, and the six/four
    form is what the merchant already sees on their own dashboard.
    """
    line = "customer said card 4111 1111 1111 1111 was declined"

    redacted = redact_text(line)

    assert "4111111111111111" not in redacted.replace(" ", "")
    assert redacted.startswith("customer said card 411111")
    assert redacted.endswith("1111 was declined")


async def test_redaction_leaves_a_long_amount_alone() -> None:
    """A sixteen-digit minor-unit amount is not a card number.

    This is the false positive that made the first version of the regex useless: it
    masked `amount_minor` on every settlement line in the access log. The Luhn check is
    what fixed it — `1234567890123456` does not pass one, and no real PAN fails one.
    """
    line = '{"amount_minor": 1234567890123456, "currency": "USD"}'

    assert redact_text(line) == line


async def test_a_cvv_is_scrubbed_by_key_name() -> None:
    """Three digits are too short to find by shape. The key name is the only handle."""
    body = '{"cvc": "123", "account_last_four": "4242"}'

    redacted = redact_text(body)

    assert '"cvc": "123"' not in redacted
    assert REDACTED in redacted
    # The last four is what operators match on in a support ticket. It stays.
    assert '"account_last_four": "4242"' in redacted


async def test_credential_headers_are_never_logged() -> None:
    """Not flag-gated, unlike PAN masking.

    There is no debugging scenario that needs a bearer token in a log file, and making it
    the same decision as PAN redaction is how one gets turned off with the other.
    """
    headers = redact_headers(
        {
            "Authorization": "Bearer sk_live_9f2",
            "X-Payzeno-Internal-Secret": "s3cr3t",
            "X-Payzeno-Service": "payzeno-api",
        }
    )

    assert headers["Authorization"] == REDACTED
    assert headers["X-Payzeno-Internal-Secret"] == REDACTED
    assert headers["X-Payzeno-Service"] == "payzeno-api"


# --------------------------------------------------------------------------------------
# ASGI plumbing for the two contextvar middleware
# --------------------------------------------------------------------------------------


def _http_scope(*, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": "/internal/v1/payouts",
        "headers": headers,
        "query_string": b"",
    }


async def _echo_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Reports the contextvars the middleware under test seeded."""
    scope.setdefault("state", {})
    scope["state"]["request_id"] = current_request_id()
    scope["state"]["correlation_id"] = current_correlation_id()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


async def _call(middleware: Any, scope: dict[str, Any]) -> dict[str, Any]:
    sent: list[dict[str, Any]] = []

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, _receive, _send)
    return dict(scope.get("state", {}))
