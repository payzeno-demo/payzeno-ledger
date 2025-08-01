"""Starlette middleware, in the order ``api-surface.md`` §1.6 contracts.

The order is not stylistic and it is asserted by ``tests/api/test_middleware.py``:

1. :class:`~app.middleware.request_id.RequestIdMiddleware`
2. :class:`~app.middleware.correlation_id.CorrelationIdMiddleware`
3. :class:`~app.middleware.access_log.StructuredAccessLogMiddleware`
4. :class:`~app.middleware.metrics.PrometheusMiddleware`
5. :class:`~app.middleware.redaction.RedactionMiddleware`
6. :class:`~app.middleware.internal_auth.InternalAuthMiddleware`

Starlette applies ``add_middleware`` in reverse — the last one added is the outermost —
so :func:`install_middleware` adds them bottom-up and nobody outside this module has to
remember that. Getting it wrong is subtle rather than loud: put the access log outside
the request id and every line logs ``request_id=None``; put the internal auth outermost
and a 401 never reaches the access log at all, which is precisely the request you want a
record of.

Layering: this package imports ``app.logging``, ``app.metrics``, ``app.ports`` and
``app.domain.ids`` and nothing else from ``app/``. No middleware touches a service, a
repository or a session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.middleware.access_log import StructuredAccessLogMiddleware
from app.middleware.correlation_id import (
    CorrelationIdMiddleware,
    bind_correlation_id,
    current_correlation_id,
    ensure_correlation_id,
)
from app.middleware.internal_auth import InternalAuthMiddleware
from app.middleware.metrics import PrometheusMiddleware
from app.middleware.redaction import RedactionMiddleware, redact_headers, redact_text
from app.middleware.request_id import (
    RequestIdMiddleware,
    bind_request_id,
    current_request_id,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, app/main.py imports us
    from fastapi import FastAPI

    from app.config import Settings
    from app.ports import FeatureFlags

#: Declared order, outermost first. Read by ``tests/api/test_middleware.py``, which
#: compares it against ``app.user_middleware`` on the built application.
MIDDLEWARE_ORDER: tuple[str, ...] = (
    "RequestIdMiddleware",
    "CorrelationIdMiddleware",
    "StructuredAccessLogMiddleware",
    "PrometheusMiddleware",
    "RedactionMiddleware",
    "InternalAuthMiddleware",
)

__all__ = [
    "MIDDLEWARE_ORDER",
    "CorrelationIdMiddleware",
    "InternalAuthMiddleware",
    "PrometheusMiddleware",
    "RedactionMiddleware",
    "RequestIdMiddleware",
    "StructuredAccessLogMiddleware",
    "bind_correlation_id",
    "bind_request_id",
    "current_correlation_id",
    "current_request_id",
    "ensure_correlation_id",
    "install_middleware",
    "redact_headers",
    "redact_text",
]


def install_middleware(
    app: "FastAPI", settings: "Settings", flags: "FeatureFlags"
) -> None:
    """Register the six middleware on ``app`` so they execute in :data:`MIDDLEWARE_ORDER`.

    Called once, from ``app/main.py::create_app``. Added in reverse because Starlette
    wraps each new middleware *around* the stack built so far.
    """
    # mTLS is terminated by nginx/payzeno-internal.conf, which only exists in the ECS
    # task definition and not in docker-compose — so the client-cert requirement follows
    # whether we are pointed at a real database or a local one. That is a proxy, not a
    # signal, and it has been on the "make this an explicit env var" list since month 4.
    app.add_middleware(
        InternalAuthMiddleware,
        secret=settings.internal_api_secret,
        require_client_cert="localhost" not in settings.database_url,
    )
    app.add_middleware(RedactionMiddleware, flags=flags)
    app.add_middleware(PrometheusMiddleware)
    app.add_middleware(StructuredAccessLogMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(RequestIdMiddleware)
