"""Every Alembic revision runs up, then down.

Two failure modes this catches, both of which have actually happened here:

1. A revision that references a table an *earlier* revision does not create yet. That is
   the mistake `apager` caught on PR #172 at 02:40 — `0020` referenced
   `settlement_duplicate_audit`, which the same PR created in a later revision. The
   revisions were reordered so the audit table became `0019`. A linear-chain assertion
   plus an actual `upgrade head` is what would have caught it at 02:26 instead.
2. A downgrade that was written by copying the upgrade and deleting half of it. Nobody
   runs downgrades in production, which is exactly why they rot.

`ledger_entry` is append-only from `0004`, so the downgrade path has to drop the trigger
before it drops the table. That ordering is asserted implicitly here: if it is wrong,
`downgrade base` raises.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]

REPO_ROOT = Path(__file__).resolve().parents[2]
REVISION_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")


@pytest.fixture(scope="module")
def alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture(scope="module")
def script_directory(alembic_config: Config) -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config)


def test_every_revision_file_is_numbered_in_sequence() -> None:
    """0001..0033, dense, no gaps, no duplicates.

    The number is not decoration — `data-model.md` §4 assigns an author per revision and
    the postmortem cites `0019` and `0020` by number.
    """
    versions = REPO_ROOT / "migrations" / "versions"
    numbers = sorted(
        int(match.group(1))
        for path in versions.iterdir()
        if (match := REVISION_FILE.match(path.name))
    )

    assert numbers, "no migration files found"
    assert numbers[0] == 1
    assert numbers == list(range(1, len(numbers) + 1)), "gap or duplicate in the chain"


def test_the_revision_chain_is_linear(script_directory: ScriptDirectory) -> None:
    """One head, one root, and every revision has exactly one parent.

    A branched chain is not a style problem here: two heads means `upgrade head` picks
    one and the other silently never runs.
    """
    heads = script_directory.get_heads()
    assert len(heads) == 1, f"expected one head, found {heads}"

    seen = 0
    for revision in script_directory.walk_revisions():
        seen += 1
        parents = revision.down_revision
        assert not isinstance(parents, tuple), f"{revision.revision} is a merge point"
    assert seen >= 33


def test_the_duplicate_audit_table_precedes_the_unique_index(
    script_directory: ScriptDirectory,
) -> None:
    """PR #172, 02:40. The one ordering bug that was caught in review.

    `0020` quarantines duplicates into `settlement_duplicate_audit` before it creates the
    unique index. If the audit table is created after it, `upgrade head` fails on an
    empty database and nobody notices until the production run at 03:05.
    """
    order = [rev.revision for rev in reversed(list(script_directory.walk_revisions()))]
    audit = next(rev for rev in order if rev.startswith("0019"))
    unique = next(rev for rev in order if rev.startswith("0020"))

    assert order.index(audit) < order.index(unique)


async def test_upgrade_head_then_downgrade_base(pg_engine, alembic_config: Config) -> None:
    """The whole chain, forwards and backwards, against a real Postgres.

    Slow — a minute or so — and marked as such. It runs in the `pytest -m integration`
    stage of CI, after the fast suite has already gone green.
    """
    from alembic import command

    async with pg_engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    def _upgrade(sync_conn) -> None:
        alembic_config.attributes["connection"] = sync_conn
        command.upgrade(alembic_config, "head")

    def _downgrade(sync_conn) -> None:
        alembic_config.attributes["connection"] = sync_conn
        command.downgrade(alembic_config, "base")

    async with pg_engine.begin() as conn:
        await conn.run_sync(_upgrade)

    async with pg_engine.connect() as conn:
        tables = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "ORDER BY tablename"
            )
        )
        names = {row[0] for row in tables}

    assert "ledger_transaction" in names
    assert "ledger_entry" in names
    assert "reconciliation_item" in names
    assert "settlement_duplicate_audit" in names
    assert "capture_attempt" in names

    async with pg_engine.begin() as conn:
        await conn.run_sync(_downgrade)

    async with pg_engine.connect() as conn:
        remaining = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        left = {row[0] for row in remaining} - {"alembic_version"}

    assert left == set(), f"downgrade left tables behind: {sorted(left)}"


async def test_idempotency_key_index_is_unique_at_head(pg_engine, alembic_config: Config) -> None:
    """`0020`, asserted at the level that matters.

    For nine months `ix_ledger_transaction_idempotency_key` existed and was NOT unique
    (`0007`, with a comment promising PAY-1188). Anyone grepping for protection found the
    index name and stopped reading. This asserts the property rather than the name.
    """
    from alembic import command

    def _upgrade(sync_conn) -> None:
        alembic_config.attributes["connection"] = sync_conn
        command.upgrade(alembic_config, "head")

    async with pg_engine.begin() as conn:
        await conn.run_sync(_upgrade)

    async with pg_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'ledger_transaction' "
                "AND indexname = 'uq_ledger_transaction_idempotency_key'"
            )
        )
        indexdef = result.scalar_one_or_none()

    assert indexdef is not None, "the unique index from 0020 is missing"
    assert "UNIQUE" in indexdef.upper()
