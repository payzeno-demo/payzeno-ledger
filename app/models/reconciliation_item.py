"""``reconciliation_item`` — one acquirer line, the atom reconciliation settles.

::

    pending ─▶ settling ─▶ settled            (terminal)
                  │
                  ├─▶ retryable ─▶ settling   (bounded: attempt_count < 6, backoff)
                  ├─▶ failed                  (terminal)
                  ├─▶ variance_exceeded       (outside merchant tolerance — manual)
                  ├─▶ needs_review            (heuristic match only — manual)
                  └─▶ orphaned                (no matching charge — manual)

``pix_reconciliation_item_retryable`` is the index the retry drain scans. Migration
``0014`` (arc PERF) created it and took the drain's p99 from 12s to 40ms — which is
exactly what made the drain fast enough to run *concurrently* with a sweep instead of
trailing it, and therefore what made PAY-2041 possible. Migration ``0024`` re-keyed it
onto ``next_attempt_at`` so the drain filters on backoff rather than ordering by
``last_attempt_at`` and hammering a degraded acquirer.

> There is **no unique index on `charge_id`** here and there must not be — a charge
> legitimately appears in two batches (original + a chargeback representment). This is
> exactly why the duplicate cannot be caught at this table, and it is why a reviewer
> looking at ``reconciliation_item`` alone concludes the design is safe.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    TimestampMixin,
    reconciliation_item_status_enum,
    reconciliation_line_type_enum,
    reconciliation_match_method_enum,
)


class ReconciliationItem(Base, TimestampMixin, LivemodeMixin):
    """One line of an acquirer settlement file."""

    __tablename__ = "reconciliation_item"
    currency: Mapped[str] = mapped_column(Currency, nullable=False)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
