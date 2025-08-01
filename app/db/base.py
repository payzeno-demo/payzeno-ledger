"""Declarative metadata for Alembic — the one module that imports every model.

``migrations/env.py`` sets ``target_metadata = app.db.base.target_metadata``. Importing
this module has the side effect of importing all 20 model classes, which is what
populates ``Base.metadata`` — without it ``alembic revision --autogenerate`` produces an
empty diff and quietly proposes dropping every table in the database.

It also carries the two schema objects that belong to no single model: the
``set_updated_at()`` trigger function every mutable table hangs off, and the
``trg_ledger_entry_immutable`` guard that makes ``ledger_entry`` append-only.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import MetaData

from app.models import (  # noqa: F401  (imported for the metadata side effect)
    Account,
    BankAccountProjection,
    BankingCalendarDay,
    CaptureAttempt,
    EventOutbox,
    FundingEvent,
    InvoiceLineStaging,
    LedgerAdjustmentRequest,
    LedgerEntry,
    LedgerTransaction,
    MerchantBalanceCache,
    MerchantProjection,
    Payout,
    ProcessedEvent,
    ReconciliationItem,
    ReconciliationRun,
    ReserveHold,
    SettlementBatch,
    SettlementCharge,
    SettlementDuplicateAudit,
)
from app.models.base import ALL_ENUM_TYPES, Base

#: What `migrations/env.py` compares the live database against.
target_metadata: Final[MetaData] = Base.metadata

#: Tables Alembic must never try to manage. `alembic_version` is Alembic's own.
EXCLUDED_TABLES: Final[frozenset[str]] = frozenset({"alembic_version"})

#: Maintains `updated_at` on every mutable table. Defined once in migration 0001 and
#: attached per-table by each migration that creates one. It lives in SQL rather than in
#: the ORM because the Alembic data migrations and `app/ops/cli.py` write rows through raw
#: SQL and must not be able to skip it.
SET_UPDATED_AT_FUNCTION: Final[str] = """
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: `ledger_entry` is append-only — no UPDATE, no DELETE, ever. Migration 0004.
LEDGER_ENTRY_IMMUTABLE_FUNCTION: Final[str] = """
CREATE OR REPLACE FUNCTION reject_ledger_entry_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entry is append-only' USING ERRCODE = 'P0001';
END;
$$ LANGUAGE plpgsql;
"""


def updated_at_trigger_sql(table: str) -> str:
    """DDL attaching :data:`SET_UPDATED_AT_FUNCTION` to `table`.

    Every migration that creates a table with an ``updated_at`` column calls this rather
    than hand-writing the trigger, so all 14 of them are spelled identically.
    """
    return (
        f"CREATE TRIGGER trg_{table}_set_updated_at "
        f"BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )


def drop_updated_at_trigger_sql(table: str) -> str:
    """The matching ``down()`` DDL. Every ``up()`` in this repo has a real ``down()``."""
    return f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table};"


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Alembic ``include_object`` hook — skip what the migrations must not manage.

    Enum types are excluded because the models declare them ``create_type=False``: the
    migrations create and drop them explicitly, and letting autogenerate emit a
    ``CREATE TYPE`` races four ECS tasks on deploy.
    """
    if type_ == "table" and name in EXCLUDED_TABLES:
        return False
    return not (type_ == "type" and name in {e.name for e in ALL_ENUM_TYPES})


def all_table_names() -> tuple[str, ...]:
    """Every table this service owns, in dependency order.

    Used by ``tests/conftest.py`` to truncate between integration tests and by
    ``app/ops/commands.py``'s ``schema`` command.
    """
    return tuple(table.name for table in target_metadata.sorted_tables)
