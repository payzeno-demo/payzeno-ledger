"""Middleware #5 — PAN redaction on anything headed for a log.

Position 5 of ``api-surface.md`` §1.6, and the ledger's slice of arc PCI. The ledger is
not supposed to see a PAN at all: it stores ``account_number_token`` and ``*_last_four``
and nothing else (``app/models/projections.py``). This exists because "not supposed to"
and "does not" are different claims, and the one that matters in an audit is the second.

Three things get scrubbed:

* request bodies buffered for the access log
* response bodies on a non-2xx, which is where an upstream error message with an echoed
  request body ends up
* the ``Authorization`` and ``X-Payzeno-Internal-Secret`` headers, always, regardless of
  the flag

The flag is ``redact_pan_in_logs`` (``FLAG_REDACT_PAN_IN_LOGS``, default on). It is
non-removable: the reason it is a flag at all is that turning it off in a staging
environment is how the payments team debugs a malformed acquirer payload, and the ledger
inherited the switch from payzeno-api rather than inventing a second mechanism.
``app/logging.py::RedactingFormatter`` is the other half — it scrubs anything a
``logger.info(...)`` call passes as a keyword. This middleware handles the bodies, which
never reach a formatter as separate fields.
"""

from __future__ import annotations

import re
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.logging import get_logger
from app.ports import FeatureFlags

logger = get_logger(__name__)

#: 13-19 digits, optionally separated by single spaces or hyphens. Deliberately greedy
#: about separators and deliberately not anchored on a card prefix: an acquirer error
#: string embeds the number in prose, and matching only on `4\d{15}` misses Amex.
PAN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

#: CVV in a JSON body. Short, so it needs the key name to be findable at all.
CVV_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'("(?:cvc|cvv|cvv2|security_code)"\s*:\s*")\d{3,4}(")', re.IGNORECASE
)

#: Header values that are never logged, flag or no flag.
SECRET_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "x-payzeno-internal-secret",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    }
)

#: Bodies larger than this are not buffered at all. A 2MB settlement import posted by
#: the Java service does not belong in a log line even fully redacted.
MAX_LOGGED_BODY_BYTES: Final[int] = 8_192

REDACTED = "[redacted]"


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn check.

    Applied so a 16-digit acquirer file reference or a ULID-adjacent numeric id is not
    mangled into ``[redacted]`` on every request. False positives here are not free:
    they destroy exactly the field an engineer is reading the log to find.
    """
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = ord(char) - 48
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _mask_pan(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = re.sub(r"[ -]", "", raw)
    if not _luhn_ok(digits):
        return raw
    return f"{digits[:6]}{'*' * (len(digits) - 10)}{digits[-4:]}"


def redact_text(value: str) -> str:
    """Mask any PAN-shaped, Luhn-valid run of digits, and any CVV-keyed field.

    Leaves the first six and last four digits: that is the BIN plus the last four the
    merchant sees on their own dashboard, and it is what makes a redacted log line still
    useful for matching a charge.
    """
    scrubbed = PAN_PATTERN.sub(_mask_pan, value)
    return CVV_PATTERN.sub(rf"\1{REDACTED}\2", scrubbed)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy with credential-bearing headers replaced.

    Not flag-gated. There is no debugging scenario that needs a bearer token in a log
    file, and treating it as the same decision as PAN redaction is how one gets turned
    off with the other.
    """
    return {
        name: (REDACTED if name.lower() in SECRET_HEADERS else redact_text(value))
        for name, value in headers.items()
    }


class RedactionMiddleware(BaseHTTPMiddleware):
    """Scrub request/response bodies before anything downstream can log them."""

    def __init__(self, app: object, *, flags: FeatureFlags) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._flags = flags

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        enabled = self._flags.enabled("redact_pan_in_logs")
        request.state.redacted_headers = (
            redact_headers(dict(request.headers)) if enabled else dict(request.headers)
        )

        response = await call_next(request)

        if not enabled or response.status_code < 400:
            return response

        # Only error responses are buffered. A 200 body is the caller's own data coming
        # straight back and nothing logs it; a 4xx/5xx body is what gets attached to a
        # support ticket.
        body = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body += chunk
            if len(body) > MAX_LOGGED_BODY_BYTES:
                break

        if body:
            request.state.redacted_error_body = redact_text(
                body.decode("utf-8", errors="replace")
            )[:MAX_LOGGED_BODY_BYTES]

        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
