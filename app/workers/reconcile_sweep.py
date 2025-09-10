"""The 900-second batch sweep.

Registered in every ledger task. Production runs four tasks, so there are four
unsynchronised sweeps, and the batch advisory lock inside
``ReconciliationService.reconcile_batch`` is what keeps them from colliding with each
other.
"""

from __future__ import annotations

import time
from typing import ClassVar

from app.config import Settings
from app.errors import PayzenoLedgerError
from app.logging import get_logger
from app.ports import SessionFactory
from app.repositories.settlement_batch import SettlementBatchRepository
