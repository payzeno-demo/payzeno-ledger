"""The one place an error response is built.

``interfaces.md`` §3.3 is explicit: exactly one
``@app.exception_handler(PayzenoLedgerError)``, rendering the ``ApiError`` envelope from
``payzeno_contracts.types``. Nothing else in the ledger constructs an error body — a
second renderer means payzeno-api's ``LedgerHttpClient`` sees two shapes for the same
condition and its ``HttpExceptionFilter`` mapping stops being total.

Two handlers are registered beyond that one:

* ``RequestValidationError`` — FastAPI's own, so a malformed body comes back as
  ``422 validation_failed`` with per-field detail in our envelope rather than FastAPI's.
* ``Exception`` — the last resort. It logs with the request id and returns
  ``500 internal_error`` with **no** detail. An unhandled exception's message is a stack
  of internals; a merchant integrator gets the request id and support gets the log.

:class:`~app.middleware.internal_auth.InternalAuthMiddleware` builds its own 401 body
because a Starlette middleware sits outside this stack entirely. That is the only
exception, and it hand-matches the same shape.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import PayzenoLedgerError, ValidationError
from app.logging import get_logger
from app.metrics import metrics
from app.middleware.correlation_id import current_correlation_id
from app.middleware.request_id import current_request_id

logger = get_logger(__name__)

#: Maps an http status onto the ``error.type`` discriminator payzeno-console switches on
#: (``interfaces.md`` §4.3). Anything unlisted is an ``api_error``.
_TYPE_BY_STATUS: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    409: "conflict_error",
    422: "validation_error",
    429: "rate_limit_error",
    502: "upstream_error",
}


def _envelope(
    *, code: str, message: str, status: int, details: dict[str, object] | None
) -> dict[str, object]:
    """Build the ``ApiError`` body. The only implementation of that shape in this repo."""
    return {
        "error": {
            "type": _TYPE_BY_STATUS.get(status, "api_error"),
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": current_request_id(),
            "correlation_id": current_correlation_id(),
        }
    }


async def handle_payzeno_error(
    request: Request, exc: PayzenoLedgerError
) -> JSONResponse:
    """Render any error in the ``app/errors.py`` tree.

    ``http_status`` and ``code`` come off the exception class, which is why the tree is
    30 classes rather than one class with a status argument: the status is a property of
    the condition, and letting a raise site choose it is how ``account_frozen`` spent two
    months returning 500 and tripping payzeno-api's breaker for a policy decision.
    """
    status = exc.http_status
    if status >= 500:
        logger.error(
            "request_failed",
            code=exc.code,
            status=status,
            path=request.url.path,
            details=exc.details,
        )
    else:
        logger.info(
            "request_rejected",
            code=exc.code,
            status=status,
            path=request.url.path,
        )
    metrics.increment("ApiError", code=exc.code, status=str(status))
    return JSONResponse(
        status_code=status,
        content=_envelope(
            code=exc.code, message=str(exc), status=status, details=exc.details
        ),
    )


async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Turn FastAPI's body/query validation failures into our envelope.

    The per-field list is preserved under ``details.fields`` in the ``FieldError`` shape
    ``types.ts`` declares — ``{field, code, message}`` — because payzeno-console renders
    it inline against the form control and a bare string does not tell it which one.
    """
    fields = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())[1:]) or "body",
            "code": str(error.get("type", "invalid")),
            "message": str(error.get("msg", "invalid value")),
        }
        for error in exc.errors()
    ]
    logger.info("request_validation_failed", path=request.url.path, fields=len(fields))
    metrics.increment("ApiError", code="validation_failed", status="422")
    return JSONResponse(
        status_code=422,
        content=_envelope(
            code="validation_failed",
            message="request validation failed",
            status=422,
            details={"fields": fields},
        ),
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Log everything, return nothing.

    Deliberately does not include ``str(exc)``: an asyncpg error message carries the
    failing SQL, and the failing SQL carries merchant ids and amounts.
    """
    logger.error(
        "request_crashed",
        path=request.url.path,
        method=request.method,
        error_class=type(exc).__name__,
        error=str(exc)[:500],
    )
    metrics.increment("ApiError", code="internal_error", status="500")
    return JSONResponse(
        status_code=500,
        content=_envelope(
            code="internal_error",
            message="an unexpected error occurred",
            status=500,
            details=None,
        ),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Wire the three handlers onto the application. Called once, by ``create_app``."""
    app.add_exception_handler(PayzenoLedgerError, handle_payzeno_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected_error)
    logger.debug(
        "error_handlers_registered",
        handlers=["PayzenoLedgerError", "RequestValidationError", "Exception"],
        # ValidationError is a subclass of PayzenoLedgerError and is handled by the
        # first entry; naming it here is only so a reader grepping for it finds this.
        subsumed=[ValidationError.__name__],
    )
