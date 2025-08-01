"""Operator tooling — the CLI that ships inside the service image.

Sits beside L6: it imports the service layer (L5) and
:class:`~app.db.session.SingleConnectionSessionFactory` (L2), and nothing imports it. It
is the **second** ``SessionFactory`` consumer in the repository — Alembic is the first —
and its existence is why ``SessionFactory`` is a Protocol with two implementations rather
than a single concrete class the pool happens to live in.

Run it as ``python -m app.ops.cli <command>`` inside the running task, or through the
``payzeno-ledger-ops`` console script. It talks to the database directly: there is no
``LEDGER_BASE_URL`` in an SSM session, and the internal auth handshake wants mTLS the CLI
cannot present.
"""

from app.ops.cli import build_parser, dispatch, main, run
from app.ops.commands import (
    cmd_backlog,
    cmd_find_duplicates,
    cmd_locks,
    cmd_outbox,
    cmd_show_batch,
    cmd_show_item,
    cmd_stale_open_batches,
    print_table,
)

__all__ = [
    "build_parser",
    "cmd_backlog",
    "cmd_find_duplicates",
    "cmd_locks",
    "cmd_outbox",
    "cmd_show_batch",
    "cmd_show_item",
    "cmd_stale_open_batches",
    "dispatch",
    "main",
    "print_table",
    "run",
]
