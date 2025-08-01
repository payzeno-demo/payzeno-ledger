"""Middleware #1 — request id.

First in the chain on purpose. Everything after it (correlation id, access log,
Prometheus, redaction, internal auth) wants a stable id to hang a log line off, and the
error handler puts it in the ``ApiError`` envelope so a merchant can quote one string
back at support.

The id is kept in a :class:`contextvars.ContextVar` rather than on ``request.state``
because the readers are not all inside the request: ``app/publishers/envelope.py`` runs
deep inside a service call with no ``Request`` in scope, and passing one down through
four layers of service constructors to reach it is exactly the plumbing this avoids.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.domain.ids import new_id

#: Header the caller may supply and that we always echo back.
REQUEST_ID_HEADER = "X-Request-Id"

#: Anything longer than this is a caller bug or an injection attempt; we mint our own.
MAX_REQUEST_ID_LENGTH = 128

_request_id: ContextVar[str | None] = ContextVar("payzeno_request_id", default=None)


def current_request_id() -> str | None:
    """The id of the request being served on this task, if any.

    Returns ``None`` outside a request — in a worker tick, a consumer poll, or the ops
    CLI. Callers must handle that; a background job has no request and pretending
    otherwise produces log lines that correlate to nothing.
    """
    return _request_id.get()


def bind_request_id(value: str) -> Token[str | None]:
    """Set the request id on this context and return the reset token.

    Exposed because ``app/consumers/base.py`` and the periodic jobs want the same
    contextvar populated with a synthetic id so their log lines join up the same way an
    HTTP request's do.
    """
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    """Undo :func:`bind_request_id`."""
    _request_id.reset(token)


def _sanitise(raw: str | None) -> str | None:
    """Accept a caller-supplied id only when it is short and printable ASCII.

    A request id ends up in structured logs and in an HTTP response header. A newline in
    it is a log-injection primitive, and a 4KB one is a cheap way to bloat every log line
    for the lifetime of a retry storm.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_REQUEST_ID_LENGTH:
        return None
    if not all(33 <= ord(char) <= 126 for char in candidate):
        return None
    return candidate


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Mint or echo ``X-Request-Id`` and seed the contextvar.

    Position 1 of the chain contracted in ``api-surface.md`` §1.6.
    """

    def __init__(self, app: object, *, header: str = REQUEST_ID_HEADER) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._header = header

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = _sanitise(request.headers.get(self._header)) or new_id("req")
        token = bind_request_id(request_id)
        # request.state is populated as well: FastAPI dependencies and route handlers
        # read it, and the contextvar is the out-of-band path for code with no Request.
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers[self._header] = request_id
        return response
