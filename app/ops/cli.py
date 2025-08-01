"""``python -m app.ops.cli`` — the operator entry point.

argparse, not click or typer: the CLI ships inside the production image and every
dependency in that image is a dependency the service carries into ECS. argparse is in the
standard library and the ergonomics gap is a few lines of subparser boilerplate.

Runs against :class:`~app.db.session.SingleConnectionSessionFactory` — one connection,
opened at start, closed at exit. It reads ``DATABASE_URL`` through the same
``app/config.py::Settings`` as the service, so there is exactly one place a connection
string comes from, and running this against production means having production's env
rather than knowing a URL.

Exit codes matter here: the on-call runbook pipes some of these into ``if`` statements.
``0`` = ran and found nothing, ``1`` = ran and found something worth looking at, ``2`` =
could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import Settings
from app.db.session import (
    SingleConnectionSessionFactory,
    create_engine,
    single_connection_factory,
)
from app.errors import PayzenoLedgerError
from app.logging import configure_logging, get_logger
from app.ops.commands import (
    cmd_backlog,
    cmd_find_duplicates,
    cmd_locks,
    cmd_outbox,
    cmd_show_batch,
    cmd_show_item,
    cmd_stale_open_batches,
)

logger = get_logger(__name__)

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    """Build the subcommand tree.

    Three of these have no help text. That is not an accident of generation — they were
    added during an incident, they work, and nobody has been back. ``locks``, ``outbox``
    and ``stale-batches`` are the ones an SRE learns from another SRE.
    """
    parser = argparse.ArgumentParser(
        prog="payzeno-ledger-ops",
        description="Operator commands for payzeno-ledger. Read-mostly; see --help per command.",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="emit structured logs to stderr as well as table output on stdout",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backlog = sub.add_parser(
        "backlog",
        help="reconciliation backlog by batch and status",
        description=(
            "Shows every non-terminal reconciliation item grouped by batch, currency and "
            "status. Exit code 1 when anything is retryable."
        ),
    )
    backlog.add_argument("--currency", default=None, help="restrict to one currency")

    item = sub.add_parser(
        "item",
        help="dump one reconciliation item as JSON",
        description=(
            "attempt_count, last_error_code and next_attempt_at between them explain "
            "almost every 'where is my settlement' question."
        ),
    )
    item.add_argument("item_id")

    duplicates = sub.add_parser(
        "duplicates",
        help="find settle transactions sharing an idempotency key",
        description=(
            "The runbook query from PAY-2041. Structurally impossible after migration "
            "0020; kept so the claim can be checked."
        ),
    )
    duplicates.add_argument("--since-hours", type=int, default=24)

    batch = sub.add_parser(
        "batch",
        help="show one settlement batch and its item counts",
    )
    batch.add_argument("batch_id")

    outbox = sub.add_parser("outbox")
    outbox.add_argument("--limit", type=int, default=20)

    sub.add_parser("locks")

    stale = sub.add_parser("stale-batches")
    stale.add_argument("--older-than-hours", type=int, default=6)

    sub.add_parser(
        "dump-openapi",
        help="print the generated OpenAPI document to stdout",
        description=(
            "What `make openapi` redirects into contracts/ledger-openapi.json. It builds "
            "the app to read its route table and touches no database, so it is the one "
            "command here that runs without DATABASE_URL — see run()."
        ),
    )

    return parser


async def dispatch(args: argparse.Namespace, sessions: SingleConnectionSessionFactory) -> int:
    """Run the selected command and turn its result into an exit code."""
    if args.command == "backlog":
        retryable = await cmd_backlog(sessions, currency=args.currency)
        return EXIT_FINDINGS if retryable else EXIT_CLEAN

    if args.command == "item":
        await cmd_show_item(sessions, args.item_id)
        return EXIT_CLEAN

    if args.command == "duplicates":
        found = await cmd_find_duplicates(sessions, since_hours=args.since_hours)
        return EXIT_FINDINGS if found else EXIT_CLEAN

    if args.command == "batch":
        await cmd_show_batch(sessions, args.batch_id)
        return EXIT_CLEAN

    if args.command == "outbox":
        pending = await cmd_outbox(sessions, limit=args.limit)
        return EXIT_FINDINGS if pending else EXIT_CLEAN

    if args.command == "locks":
        held = await cmd_locks(sessions)
        return EXIT_FINDINGS if held else EXIT_CLEAN

    if args.command == "stale-batches":
        stale = await cmd_stale_open_batches(
            sessions, older_than_hours=args.older_than_hours
        )
        return EXIT_FINDINGS if stale else EXIT_CLEAN

    print(f"unknown command {args.command!r}", file=sys.stderr)
    return EXIT_ERROR


async def run(argv: list[str] | None = None) -> int:
    """Parse, connect, dispatch, disconnect.

    The engine is disposed in a ``finally`` so a command that raises still gives its
    connection back — an operator hitting Ctrl-C halfway through a backlog scan should
    not leave an idle-in-transaction connection behind on a database that is already
    having a bad night.

    ``single_connection_factory`` checks out exactly one connection and wraps it in the
    non-pooled :class:`SingleConnectionSessionFactory`. One command, one connection: this
    process shares a database with four production tasks and it is not entitled to a pool.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, json_output=bool(args.json_logs))

    # Handled before the engine exists. Generating the OpenAPI document only needs the
    # route table, and `make openapi` has to work on a laptop with no database — which is
    # exactly where somebody regenerating a snapshot after renaming a route is sitting.
    if args.command == "dump-openapi":
        from app.main import create_app

        json.dump(create_app(settings).openapi(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return EXIT_CLEAN

    engine = create_engine(settings)
    try:
        async with single_connection_factory(engine) as sessions:
            assert isinstance(sessions, SingleConnectionSessionFactory)
            return await dispatch(args, sessions)
    except PayzenoLedgerError as exc:
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        logger.error("ops_command_failed", command=args.command, code=exc.code)
        return EXIT_ERROR
    finally:
        await engine.dispose()


def main() -> None:
    """Console entry point. Wired in ``pyproject.toml`` as ``payzeno-ledger-ops``."""
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":  # pragma: no cover
    main()
