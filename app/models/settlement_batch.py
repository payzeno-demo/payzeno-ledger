"""``settlement_batch`` — the unit of money actually arriving from an acquirer.

One batch = one acquirer settlement file = one currency = one calendar processing day.

::

    open ─close─▶ closed ─start_reconcile─▶ reconciling ─▶ reconciled ─fund─▶ funded
                                                 │
                                                 ├─▶ partially_reconciled
                                                 └─▶ failed

``partially_reconciled`` is the state that keeps a retry backlog alive, and is therefore
the state arc INC's race needs. It is reachable and it is normal — two batches sat in it
for six hours on the night of PAY-2041.

``funded`` means a ``funding_event`` — an actual bank credit — matched within
``FUNDING_MATCH_TOLERANCE_BPS`` and ``settlement_funding`` has posted.
``PayoutCalculator.compute_available`` counts **only funded batches**, so a short-paying
acquirer cannot leave Payzeno paying merchants out of money that never arrived.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar, Final

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    Currency,
    LivemodeMixin,
    TimestampMixin,
    acquirer_enum,
    settlement_batch_status_enum,
)

#: Statuses `ReconciliationSweepJob` picks up. `closed` is a first pass;
#: `partially_reconciled` is a batch with retryable items left over.
SWEEPABLE_STATUSES: Final[tuple[str, ...]] = ("closed", "partially_reconciled")

#: Terminal-ish: nothing sweeps these again without an operator.
CLOSED_STATUSES: Final[frozenset[str]] = frozenset({"funded", "failed"})


class SettlementBatch(Base, TimestampMixin, LivemodeMixin):
    """One acquirer settlement file, and its reconciliation lifecycle."""

    __tablename__ = "settlement_batch"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    processing_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    def overposted_ratio(self) -> float:
        """``posted_total_minor / expected_total_minor``, or 0.0 when nothing is expected.

        PAY-2055's ``ledger_batch_overposted`` alarm fires above 1.001. On the night of
        PAY-2041 this ratio reached 1.09 on ``sb_…QK`` and nothing was watching it.
        """
        if self.expected_total_minor == 0:
            return 0.0
        return self.posted_total_minor / self.expected_total_minor
