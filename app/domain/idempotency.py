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

def canonical_json(payload: Any) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace, no NaN."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


