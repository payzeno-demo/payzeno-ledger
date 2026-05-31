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
    widths = [
        max(len(str(headers[index])), *(len(row[index]) for row in cells))
        for index in range(len(headers))
    ]
    line = "  ".join(str(headers[i]).ljust(widths[i]) for i in range(len(headers)))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in cells:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    print(f"\n{len(cells)} row(s)")


async def cmd_backlog(
    sessions: SingleConnectionSessionFactory, *, currency: str | None = None
) -> int:
    """Show the reconciliation backlog broken down by batch and status.

    The number that went from single digits to 4,113 on the night of PAY-2041. Reads the
    same aggregate the ``/internal/v1/reconciliation/backlog`` route does, without
    needing the route.
    """
    items = ReconciliationItemRepository()
    async with sessions.begin() as session:
        buckets = await items.aggregate_backlog(session, currency=currency)

    print_table(
        ("batch_id", "currency", "status", "items", "oldest_next_attempt_at", "gross_minor"),
        [
            (
                bucket.batch_id,
                bucket.currency,
                bucket.status,
                bucket.item_count,
                bucket.oldest_next_attempt_at,
                bucket.gross_minor,
            )
            for bucket in buckets
        ],
    )
    return sum(
        bucket.item_count for bucket in buckets if bucket.status in RETRYABLE_STATUSES
    )


async def cmd_show_item(
    sessions: SingleConnectionSessionFactory, item_id: str
) -> None:
    """Dump one reconciliation item as JSON.

    The first thing anyone runs when a merchant asks why a settlement is missing.
    ``attempt_count``, ``last_error_code`` and ``next_attempt_at`` between them explain
    almost every case.
    sessions: SingleConnectionSessionFactory, *, since_hours: int = 24
) -> int:
    """Find settle transactions sharing an idempotency key.

    The query from ``docs/runbooks/reconciliation.md``, and the one that produced the
    1,847 figure in the postmortem. It exists as a command because at 01:20 nobody wants
    to be reconstructing a ``GROUP BY … HAVING count(*) > 1`` from memory against a
    replica.

    Structurally impossible after migration ``0020`` made
    ``ix_ledger_transaction_idempotency_key`` unique. Kept for the historical window and
    because "impossible" is a claim worth being able to check.
    transactions = LedgerTransactionRepository()
    async with sessions.begin() as session:
        rows = await transactions.list_duplicate_idempotency_keys(session, since=since)

    print_table(
        ("idempotency_key", "count", "transaction_ids", "merchant_id", "amount_minor"),
        [
            (
                row.idempotency_key,
                row.count,
                ",".join(row.transaction_ids),
                row.merchant_id,
                row.amount_minor,
            )
            for row in rows
        ],
    )
    return len(rows)


async def cmd_show_batch(
    sessions: SingleConnectionSessionFactory, batch_id: str
) -> None:
    """Show one settlement batch and its item counts by status."""
    batches = SettlementBatchRepository()
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
    outbox = EventOutboxRepository()
    async with sessions.begin() as session:
        pending = await outbox.list_unpublished(session, limit=limit, max_attempts=99)

    print_table(
        ("id", "event_type", "attempts", "created_at", "last_error"),
        [
            (row.id, row.event_type, row.publish_attempts, row.created_at, row.last_error)
            for row in pending
        ],
    )
    return len(pending)


async def cmd_locks(sessions: SingleConnectionSessionFactory) -> int:
    async with sessions.begin() as session:
        result = await session.execute(
            text(
                """
                SELECT l.classid, l.objid, l.granted, a.state,
                       a.query_start, left(a.query, 80) AS query
                  FROM pg_locks l
                  JOIN pg_stat_activity a ON a.pid = l.pid
                 WHERE l.locktype = 'advisory'
                 ORDER BY a.query_start
                """
            )
        )
        rows = list(result)

    print_table(
        ("classid", "objid", "granted", "state", "query_start", "query"),
        [tuple(row) for row in rows],
    )
    return len(rows)


async def cmd_stale_open_batches(
    sessions: SingleConnectionSessionFactory, older_than_hours: int = 6
) -> int:
    batches = SettlementBatchRepository()
    async with sessions.begin() as session:
        open_batches = await batches.list_by_status(session, ("open",))
        stale = [b for b in open_batches if b.processing_date <= cutoff]

    print_table(
        ("id", "acquirer", "currency", "processing_date", "item_count", "gross_minor"),
        [
            (b.id, b.acquirer, b.currency, b.processing_date, b.item_count, b.gross_minor)
            for b in stale
        ],
    )
    return len(stale)
