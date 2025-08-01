"""Middleware #4 — the Prometheus histograms behind ``GET /metrics``.

Position 4 of ``api-surface.md`` §1.6. ``/metrics`` itself is served by the
``prometheus_client`` ASGI app mounted in ``app/main.py``; this middleware is what puts
anything in it.

Labels are the route *template* and nothing else that varies per request. A ULID in a
Prometheus label is a new time series per settlement item, and 4,113 retryable items in
one night is 4,113 series that never get scraped again — which is how you find out
Prometheus has a cardinality budget.
"""

from __future__ import annotations

import time
from typing import Final

from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.middleware.access_log import _route_template

#: Seconds. Tuned for an internal service on the same VPC: the interesting range is
#: 5ms-1s, and the long tail is the acquirer, not us.
LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

REQUEST_COUNT = Counter(
    "payzeno_ledger_http_requests_total",
    "Requests handled by payzeno-ledger, by route template and status class.",
    labelnames=("method", "route", "status"),
)

REQUEST_LATENCY = Histogram(
    "payzeno_ledger_http_request_duration_seconds",
    "Wall time spent handling a request, by route template.",
    labelnames=("method", "route"),
    buckets=LATENCY_BUCKETS,
)

REQUESTS_IN_FLIGHT = Gauge(
    "payzeno_ledger_http_requests_in_flight",
    "Requests currently being handled, by route template.",
    labelnames=("method", "route"),
)

EXCEPTIONS = Counter(
    "payzeno_ledger_http_exceptions_total",
    "Requests that left the handler by raising, by exception class.",
    labelnames=("method", "route", "exception"),
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Count and time every request, excluding the scrape endpoint itself."""

    def __init__(self, app: object, *, exclude: frozenset[str] | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._exclude = exclude if exclude is not None else frozenset({"/metrics"})

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in self._exclude:
            return await call_next(request)

        # Resolved before the call for the in-flight gauge and again after it for the
        # counter: before routing has run the template is not known yet, so an
        # in-flight request is attributed to its raw path and a completed one to its
        # route. That asymmetry is deliberate — the gauge is a liveness signal, the
        # counter is the one that has to stay low-cardinality.
        method = request.method
        pre_route = request.url.path
        REQUESTS_IN_FLIGHT.labels(method=method, route=pre_route).inc()
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            EXCEPTIONS.labels(
                method=method,
                route=_route_template(request),
                exception=type(exc).__name__,
            ).inc()
            raise
        finally:
            REQUESTS_IN_FLIGHT.labels(method=method, route=pre_route).dec()

        route = _route_template(request)
        REQUEST_LATENCY.labels(method=method, route=route).observe(
            time.perf_counter() - started
        )
        REQUEST_COUNT.labels(
            method=method, route=route, status=str(response.status_code)
        ).inc()
        return response
