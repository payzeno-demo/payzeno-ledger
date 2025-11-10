"""Internal ledger idempotency keys and request fingerprints.

`domain-model.md` §0.3(b). Every ``LedgerTransaction`` carries a **deterministic**
``idempotency_key`` derived from the business fact — never from an attempt counter, a
clock, or a random value. Format::

    <purpose>:<scope_id>:<subject_id>

Both ids are prefixed ULIDs. Two shapes are load-bearing and are enforced here:

* ``settle``'s subject is the ``reconciliation_item`` id, not the charge id. A charge
  legitimately appears in two batches (original + representment) and an acquirer file
  carries non-sale lines with no charge at all.
* There is no ``fee:<batch>:<charge>``. Per-charge processing fees are booked by the
  ``capture`` posting and nothing else; a ``fee:`` key coexisting with a ``settle:`` key
  for the same pair is how revenue gets double counted.

> The uniqueness of this key is the only thing standing between Payzeno and a duplicate
> settlement. The index enforcing it was non-unique until migration ``0020``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from app.domain import ids
from app.errors import ValidationError

#: Purposes whose scope must be a batch id and whose subject must be an item id.
_ITEM_SCOPED: Final[frozenset[str]] = frozenset({"settle"})

def parse_key(key: str) -> tuple[str, str, str]:
    """Split a ledger key back into ``(purpose, scope_id, subject_id)``.

    Used by the duplicate-quarantine query in the ops CLI and by the runbook's
    duplicate-finding report, both of which need the batch out of a bare key string.
    """
    parts = key.split(":")
    if len(parts) != 3:
        raise ValidationError("malformed ledger key", details={"key": key})
    purpose, scope_id, subject_id = parts
    if not purpose or not scope_id:
        raise ValidationError("malformed ledger key", details={"key": key})
    return purpose, scope_id, subject_id


def canonical_json(payload: Any) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace, no NaN."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def fingerprint_of(item: Any) -> str:
    """Request fingerprint for a settlement posting.

    Stored in ``ledger_transaction.request_fingerprint`` (added by migration ``0020``) and
    compared by ``LedgerTransactionRepository.claim_idempotency_key``. It is what makes
    ``POST /internal/v1/transactions``'s "200 on an identical body, 409 on a repeat with a
    different body" implementable — without a stored value there is nothing to compare.

    Deliberately excludes ``attempt_count``, ``last_attempt_at``, ``status`` and
    ``next_attempt_at``: a retry of the same acquirer line is the *same* request, and
    hashing the attempt counter would make every retry look like a body divergence.
    """
    if isinstance(item, dict):
        return sha256_of(item)
    return sha256_of(
        {
            "batch_id": getattr(item, "batch_id", None),
            "item_id": getattr(item, "id", None),
            "charge_id": getattr(item, "charge_id", None),
            "merchant_id": getattr(item, "merchant_id", None),
            "line_type": getattr(item, "line_type", None),
            "currency": getattr(item, "currency", None),
            "livemode": getattr(item, "livemode", None),
            "gross_minor": getattr(item, "gross_minor", None),
            "fee_minor": getattr(item, "fee_minor", None),
            "net_minor": getattr(item, "net_minor", None),
            "interchange_minor": getattr(item, "interchange_minor", None),
            "scheme_fee_minor": getattr(item, "scheme_fee_minor", None),
            "acquirer_reference": getattr(item, "acquirer_reference", None),
        }
    )


