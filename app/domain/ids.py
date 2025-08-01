"""Prefixed ULID identifiers.

`domain-model.md` §0.2: every public identifier is ``<prefix>_<26-char Crockford ULID>``.
Prefixing is centralised here — payzeno-api has ``src/common/ids/id.factory.ts`` and
payzeno-billing-legacy has ``com.payzeno.billing.common.Ids``; this is the third of the
three, and the only one that may mint a ledger-owned prefix.
"""

from __future__ import annotations

import re
from typing import Final

from ulid import ULID

from app.errors import ValidationError

#: Prefixes this service owns and may generate.
ACCOUNT: Final[str] = "acct"
LEDGER_TRANSACTION: Final[str] = "txn"
LEDGER_ENTRY: Final[str] = "le"
SETTLEMENT_BATCH: Final[str] = "sb"
RECONCILIATION_RUN: Final[str] = "rr"
RECONCILIATION_ITEM: Final[str] = "ri"
PAYOUT: Final[str] = "po"
CAPTURE_ATTEMPT: Final[str] = "cap"
FUNDING_EVENT: Final[str] = "fe"
RESERVE_HOLD: Final[str] = "rh"
ADJUSTMENT_REQUEST: Final[str] = "lar"
OUTBOX_EVENT: Final[str] = "evt"
INVOICE_LINE_STAGING: Final[str] = "ils"

OWNED_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        ACCOUNT,
        LEDGER_TRANSACTION,
        LEDGER_ENTRY,
        SETTLEMENT_BATCH,
        RECONCILIATION_RUN,
        RECONCILIATION_ITEM,
        PAYOUT,
        CAPTURE_ATTEMPT,
        FUNDING_EVENT,
        RESERVE_HOLD,
        ADJUSTMENT_REQUEST,
        OUTBOX_EVENT,
        INVOICE_LINE_STAGING,
    }
)

#: Prefixes minted by payzeno-api. The ledger reads them, stores them, never generates them.
FOREIGN_PREFIXES: Final[frozenset[str]] = frozenset(
    {"mch", "usr", "key", "ba", "pi", "ch", "re", "dp", "inv", "sub"}
)

_ULID_BODY: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_PREFIX: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2,5}$")


def new_id(prefix: str) -> str:
    """Mint a fresh prefixed ULID.

    Refuses to mint a prefix this service does not own — a ``ch_`` minted here would be a
    charge id the ledger invented, and the projection would never match it.
    """
    if prefix not in OWNED_PREFIXES:
        raise ValidationError(
            "payzeno-ledger does not own this id prefix",
            details={"prefix": prefix, "owned": sorted(OWNED_PREFIXES)},
        )
    return f"{prefix}_{ULID()}"


def split_id(value: str) -> tuple[str, str]:
    """Split ``"txn_01H…"`` into ``("txn", "01H…")``. Raises on anything malformed."""
    if not isinstance(value, str) or "_" not in value:
        raise ValidationError("not a prefixed id", details={"value": str(value)})
    prefix, _, body = value.partition("_")
    if not _PREFIX.match(prefix) or not _ULID_BODY.match(body):
        raise ValidationError("not a prefixed id", details={"value": value})
    return prefix, body


def prefix_of(value: str) -> str:
    """The prefix half of a prefixed id."""
    return split_id(value)[0]


def is_valid(value: str, *, prefix: str | None = None) -> bool:
    """True when `value` parses, and (optionally) carries the expected prefix."""
    try:
        actual, _ = split_id(value)
    except ValidationError:
        return False
    return prefix is None or actual == prefix


def require(value: str, prefix: str) -> str:
    """Assert that `value` is an id of `prefix` and return it unchanged.

    Used at the API boundary and by ``ledger_key`` so a caller cannot slip an acquirer
    reference into a slot the idempotency key format reserves for a ULID.
    """
    actual, _ = split_id(value)
    if actual != prefix:
        raise ValidationError(
            "unexpected id prefix",
            details={"value": value, "expected": prefix, "actual": actual},
        )
    return value


def timestamp_ms(value: str) -> int:
    """Extract the ULID's embedded millisecond timestamp.

    The ops CLI uses this to age rows without joining ``created_at``, and the audit
    service uses it to order transactions that share a ``posted_at`` to the second.
    """
    _, body = split_id(value)
    return ULID.from_str(body).timestamp
