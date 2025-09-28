"""The batch sweep.

One pass over every eligible item in one settlement batch, serialised against other
sweeps of the same batch by a **batch-scoped Postgres advisory lock** (PAY-1402). The
lock lives in its own guard transaction that outlives every per-item transaction, so a
5,000-item pass never holds 5,000 rows' worth of locks.

Session usage in one pass, deliberately:

* ``guard``  — holds ``pg_advisory_xact_lock(PAY, hash(batch_id))`` for the pass
* ``read``   — lists the eligible items once
* per item   — one short transaction each, so a failure rolls back one item

That is three concurrent sessions from one shared repository instance, which is why
``BaseRepository`` is stateless and why ``DATABASE_POOL_SIZE`` is 20.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.locks import AdvisoryLockManager
from app.errors import PayzenoLedgerError, RetryableSettlementError
from app.logging import get_logger
from app.metrics import metrics
from app.models.reconciliation_run import ReconciliationRun
from app.ports import Clock, EventPublisher, SessionFactory
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.reconciliation_run import ReconciliationRunRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.services.reconciliation.constants import (
