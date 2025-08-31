"""Operator commands. The bodies behind ``python -m app.ops.cli``.

This module and Alembic are the only consumers of
:class:`~app.db.session.SingleConnectionSessionFactory`. Everything else in the service
goes through the pooled factory. The CLI runs one connection because it runs one thing at
a time and because an operator on a laptop through a bastion should not be taking twenty
connections out of a pool the running service is sharing with four tasks.

Written against repositories and services directly rather than against the HTTP surface —
there is no ``LEDGER_BASE_URL`` in an SSM session and the internal auth handshake needs
mTLS the CLI does not have.

# TODO: this should live in a runbook. Half of what is here is a query somebody typed at
# 02:00 and then wanted again the following week; the useful half should be in
# docs/runbooks/reconciliation.md with the psql text, and the rest should be deleted.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import text

from app.db.session import SingleConnectionSessionFactory
from app.logging import get_logger
from app.repositories.ledger_transaction import LedgerTransactionRepository
from app.repositories.outbox import EventOutboxRepository
from app.repositories.reconciliation_item import ReconciliationItemRepository
from app.repositories.settlement_batch import SettlementBatchRepository
from app.services.reconciliation.constants import RETRYABLE_STATUSES

logger = get_logger(__name__)


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """Fixed-width table on stdout.

    No dependency on rich or tabulate: the CLI is run inside the service image, which is
    a slim Python base with the service's own dependencies and nothing else. Adding a
    formatting library to the production image so an operator gets nicer column
    alignment is not a trade worth making.
    """
    if not rows:
        print("(no rows)")
        return
    cells = [[("" if value is None else str(value)) for value in row] for row in rows]
    items = ReconciliationItemRepository()
    async with sessions.begin() as session:
        batch = await batches.get_or_raise(session, batch_id)
        totals = await items.totals_for_batch(session, batch_id)

    print_table(
        ("field", "value"),
        [
            ("id", batch.id),
            ("acquirer", batch.acquirer),
            ("currency", batch.currency),
            ("processing_date", batch.processing_date),
            ("file_reference", batch.file_reference),
            ("status", batch.status),
            ("item_count", batch.item_count),
            ("gross_minor", batch.gross_minor),
            ("net_minor", batch.net_minor),
            ("funded_minor", batch.funded_minor),
            ("funded_at", batch.funded_at),
            ("reconciled_at", batch.reconciled_at),
        ],
    )
    print()
    print_table(
        ("status", "items"), [(status, count) for status, count in sorted(totals.items())]
    )


async def cmd_outbox(sessions: SingleConnectionSessionFactory, limit: int = 20) -> int:
