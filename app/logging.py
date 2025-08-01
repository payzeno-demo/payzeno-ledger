"""Structured logging, and the PCI backstop that lives with it.

Two exports matter. :func:`get_logger` is called at the top of sixty-two modules and
returns a structlog bound logger, so every log line in this service is
``logger.info("event_name", key=value, ...)`` and never an f-string. Event names are
snake_case nouns-with-verbs (``settlement_item_retryable``, ``job_registered``) because
they are grepped and alerted on, and a message that interpolates an id is a message you
cannot count.

:class:`RedactingFormatter` is the other half of arc PCI.
``app/middleware/redaction.py`` scrubs request and response bodies before they reach the
access log; this scrubs whatever gets past it — an acquirer error body echoed into a
warning, a repr of a model, a traceback with a bound parameter in it. The ledger is not
supposed to hold a PAN at all (``app/models/projections.py`` stores
``account_number_token`` and ``*_last_four``), and this is a backstop, not a licence.

Layering: L-1. Imports ``app.ports``/``app.config`` for types only; nothing above L-1.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Final

import structlog

__all__ = [
    "PAN_PATTERN",
    "RedactingFormatter",
    "configure_logging",
    "get_logger",
    "scrub",
]

#: 13-19 digits with optional separators. Deliberately greedy about separators and
#: deliberately not Luhn-checked: a false positive costs a redacted order number, a false
#: negative costs a PCI finding, and that trade is not close.
PAN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:\d[ -]?){13,19}\b")

#: Keys whose values never appear in a log line whatever they contain.
SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "account_number",
        "api_key",
        "authorization",
        "card_number",
        "iban",
        "internal_api_secret",
        "pan",
        "routing_number",
        "secret",
        "x-payzeno-internal-secret",
    }
)

REDACTED: Final[str] = "[redacted]"


def scrub(value: str) -> str:
    """Replace anything PAN-shaped in ``value``.

    Applied to the rendered line rather than to individual fields, because the field that
    leaks is never the one called ``pan`` — it is ``body``, ``detail`` or the string form
    of an exception raised inside httpx.
    """
    return PAN_PATTERN.sub(REDACTED, value)


class RedactingFormatter(logging.Formatter):
    """A stdlib formatter that scrubs its own output.

    structlog handles the fields; this catches everything that reaches the stdlib root
    logger from a dependency — SQLAlchemy echo, botocore, httpx, uvicorn's own access log
    if someone re-enables it. Installed unconditionally, and *not* behind the
    ``redact_pan_in_logs`` flag: the flag gates the request-body middleware, where turning
    redaction off is a debugging decision with a blast radius somebody signed for. Here it
    would just be a way to lose the backstop.
    """

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def _redact_event_dict(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: drop sensitive keys, scrub string values."""
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
        elif isinstance(event_dict[key], str):
            event_dict[key] = scrub(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install the processor chain. Called once, from ``create_app`` and from the ops CLI.

    ``json_output`` is False for the CLI when a human is watching — an operator reading a
    trial-balance failure at 02:00 should not be piping through ``jq`` — and True
    everywhere else, because the ECS log driver ships to CloudWatch Logs Insights and
    Insights cannot query a console-rendered line.

    Idempotent: calling it twice reconfigures rather than stacking processors, which
    matters because ``tests/api/conftest.py`` builds an app per module.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_event_dict,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level) if isinstance(level, str) else level
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """The module-level logger every file in ``app/`` opens with.

    Bound to ``__name__`` so the emitting module is a field rather than something to be
    inferred from the event name — ``settlement_item_retryable`` is raised from both
    ``poster.py`` and, in a different shape, from the ops CLI.
    """
    return structlog.get_logger(name)
