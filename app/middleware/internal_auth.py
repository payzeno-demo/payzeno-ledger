"""Middleware #6 — the internal-only guard.

Position 6, last in the chain contracted in ``api-surface.md`` §1.6, and the belt to
``app/api/deps.py::require_internal_service``'s braces. The dependency makes the auth
requirement visible on each handler; this middleware is what actually enforces the
exemption list, so a route added without the dependency is closed by default rather than
open by default.

Three credentials, all required together:

* mTLS — terminated by the internal nginx listener, which forwards the verified subject
  in ``X-Payzeno-Client-Cert-Subject``. We do not verify the certificate ourselves.
* ``X-Payzeno-Service`` — the caller's own name, checked against an allow-list so a
  compromised console token cannot masquerade as payzeno-api.
* ``X-Payzeno-Internal-Secret`` — ``INTERNAL_API_SECRET``, compared in constant time.

The three unauthenticated operational routes (``api-surface.md`` §10.5) are exempt
because an ECS health check and a Prometheus scraper cannot present any of the above,
and they carry no merchant data.
"""

from __future__ import annotations

import hmac
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.logging import get_logger
from app.metrics import metrics
from app.middleware.request_id import current_request_id

logger = get_logger(__name__)

#: Exactly the three routes in ``api-surface.md`` §10.5. Not a prefix match: a prefix
#: would make ``/metrics-debug`` public too, and someone would eventually add one.
PUBLIC_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/readyz", "/metrics"})

#: Who is allowed to call the internal surface at all. The ledger calls nobody inside
#: Payzeno, so this list only ever shrinks.
ALLOWED_SERVICES: Final[frozenset[str]] = frozenset(
    {"payzeno-api", "payzeno-billing-legacy", "payzeno-ops"}
)

SERVICE_HEADER = "X-Payzeno-Service"
SECRET_HEADER = "X-Payzeno-Internal-Secret"
CLIENT_CERT_HEADER = "X-Payzeno-Client-Cert-Subject"


class InternalAuthMiddleware(BaseHTTPMiddleware):
    """Reject anything that is not a known internal caller."""

    def __init__(
        self,
        app: object,
        *,
        secret: str,
        allowed_services: frozenset[str] = ALLOWED_SERVICES,
        require_client_cert: bool = True,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._secret = secret
        self._allowed = allowed_services
        # False in docker-compose, where there is no mTLS terminator in front of us.
        self._require_client_cert = require_client_cert

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        rejection = self._check(request)
        if rejection is not None:
            metrics.increment(
                "InternalAuthRejected", reason=rejection, path=request.url.path
            )
            logger.warning(
                "internal_auth_rejected",
                reason=rejection,
                path=request.url.path,
                caller=request.headers.get(SERVICE_HEADER, "unknown"),
                request_id=current_request_id(),
            )
            return self._denied(rejection)

        request.state.calling_service = request.headers[SERVICE_HEADER]
        return await call_next(request)

    def _check(self, request: Request) -> str | None:
        """Return a rejection reason, or ``None`` when the caller is legitimate."""
        service = request.headers.get(SERVICE_HEADER)
        if not service:
            return "missing_service_header"
        if service not in self._allowed:
            return "unknown_service"

        presented = request.headers.get(SECRET_HEADER, "")
        if not presented:
            return "missing_secret"
        if not hmac.compare_digest(presented, self._secret):
            return "bad_secret"

        if self._require_client_cert and not request.headers.get(CLIENT_CERT_HEADER):
            return "missing_client_cert"
        return None

    @staticmethod
    def _denied(reason: str) -> JSONResponse:
        """The one place this middleware builds a body.

        Deliberately *not* routed through ``app/api/error_handlers.py``: a Starlette
        middleware sits outside the FastAPI exception-handler stack, so raising
        ``PayzenoLedgerError`` here would escape as a bare 500. The shape still matches
        the ``ApiError`` envelope so the caller's parser does not have to special-case
        it.
        """
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "type": "authentication_error",
                    "code": "authentication_required",
                    "message": "internal service authentication failed",
                    "details": {"reason": reason},
                    "request_id": current_request_id(),
                }
            },
        )
