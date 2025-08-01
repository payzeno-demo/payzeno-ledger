"""Middleware #2 — correlation id.

``events.ts`` requires ``EventEnvelope.correlation_id`` to be the id of the HTTP request
that *originated* the causal chain, not of the request that happens to be publishing.
payzeno-api stamps it on the events it publishes, its consumers forward it onto the SQS
message attributes, and ``app/consumers/base.py`` re-binds it before dispatching a
handler. This middleware is the HTTP half of that: it reads ``X-Correlation-Id`` off the
inbound request, falls back to the request id, and seeds the contextvar that
``app/publishers/envelope.py::build_envelope`` reads.

There is exactly one reader of the contextvar and exactly one writer of the header. If a
second place starts minting correlation ids, the whole chain stops joining up and the
only symptom is a support ticket nobody can trace.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.domain.ids import new_id
from app.middleware.request_id import current_request_id

#: Optional inbound header. payzeno-api always sends it; the ops console does not.
CORRELATION_ID_HEADER = "X-Correlation-Id"

MAX_CORRELATION_ID_LENGTH = 128

_correlation_id: ContextVar[str | None] = ContextVar(
    "payzeno_correlation_id", default=None
)


def current_correlation_id() -> str | None:
    """The correlation id in scope, or ``None``.

    ``build_envelope`` treats ``None`` as "this chain starts here" and mints a fresh
    ``cor_`` id. That is the correct behaviour for a sweep tick or a settlement import:
    nothing upstream caused them.
    """
    return _correlation_id.get()


def bind_correlation_id(value: str) -> Token[str | None]:
    """Seed the contextvar and return the reset token.

    Used by the HTTP middleware, by :class:`app.consumers.base.BaseConsumer` when it
    unpacks an envelope, and by :class:`app.workers.base.PeriodicJob` subclasses that
    want one id across a whole pass.
    """
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Undo :func:`bind_correlation_id`."""
    _correlation_id.reset(token)


def ensure_correlation_id(prefix: str = "cor") -> str:
    """Return the correlation id in scope, minting and binding one if there is none.

    Background code paths call this at the top of a unit of work so every event they
    publish during it shares one id.
    """
    existing = current_correlation_id()
    if existing is not None:
        return existing
    minted = new_id(prefix)
    bind_correlation_id(minted)
    return minted


def _sanitise(raw: str | None) -> str | None:
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_CORRELATION_ID_LENGTH:
        return None
    if not all(33 <= ord(char) <= 126 for char in candidate):
        return None
    return candidate


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Read ``X-Correlation-Id``, fall back to the request id, seed the contextvar.

    Position 2 of the chain contracted in ``api-surface.md`` §1.6 — after
    :class:`~app.middleware.request_id.RequestIdMiddleware`, because the fallback is the
    request id and it has to exist by the time we look for it.
    """

    def __init__(self, app: object, *, header: str = CORRELATION_ID_HEADER) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._header = header

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        correlation_id = (
            _sanitise(request.headers.get(self._header))
            or current_request_id()
            or new_id("cor")
        )
        token = bind_correlation_id(correlation_id)
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        finally:
            reset_correlation_id(token)
        # Echoed so the caller can log the value we actually used, which is not
        # necessarily the value it sent — see _sanitise.
        response.headers[self._header] = correlation_id
        return response
