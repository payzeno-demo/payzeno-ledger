"""Middleware #3 — one structured access log line per request.

Position 3 of ``api-surface.md`` §1.6. It sits after the two id middlewares so every
line carries ``request_id`` and ``correlation_id``, and before the Prometheus middleware
so a request that blows up inside instrumentation still gets logged.

The line is emitted in a ``finally``: an unhandled exception must still produce an access
log entry, otherwise the one request that mattered is the one request with no record of
it. During PAY-2041 the ledger's access log is how the timeline of the retry route was
reconstructed, which is why the item id is pulled out of the path into its own field.
"""

from __future__ import annotations

import time
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.logging import get_logger
from app.middleware.correlation_id import current_correlation_id
from app.middleware.request_id import current_request_id

logger = get_logger(__name__)

#: Paths we do not log. Prometheus scrapes /metrics every 15s and the ECS health check
#: hits /healthz every 10s; between them that is ~14k lines a day saying nothing.
QUIET_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/readyz", "/metrics"})

#: Anything slower than this gets logged at warning level, whatever its status code.
SLOW_REQUEST_MS: Final[int] = 2_000


def _route_template(request: Request) -> str:
    """The path *pattern*, not the concrete path.

    ``/internal/v1/reconciliation/items/rci_01HX.../retry`` is useless as a log
    dimension and catastrophic as a Prometheus label. Starlette puts the matched
    ``APIRoute`` on the scope once routing has happened; before that (a 404) we fall
    back to the raw path.
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(path_format, str):
        return path_format
    return request.url.path


class StructuredAccessLogMiddleware(BaseHTTPMiddleware):
    """Emit exactly one structlog line per request."""

    def __init__(self, app: object, *, service: str = "payzeno-ledger") -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._service = service

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in QUIET_PATHS:
            return await call_next(request)

        started = time.monotonic()
        status = 500
        outcome = "error"
        try:
            response = await call_next(request)
            status = response.status_code
            outcome = "ok" if status < 500 else "error"
            return response
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            fields = {
                "service": self._service,
                "method": request.method,
                "path": request.url.path,
                "route": _route_template(request),
                "status": status,
                "duration_ms": duration_ms,
                "request_id": current_request_id(),
                "correlation_id": current_correlation_id(),
                "caller": request.headers.get("X-Payzeno-Service", "unknown"),
                "outcome": outcome,
            }
            if status >= 500:
                logger.error("http_request", **fields)
            elif duration_ms >= SLOW_REQUEST_MS:
                logger.warning("http_request_slow", **fields)
            else:
                logger.info("http_request", **fields)
