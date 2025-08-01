"""Declarative base, shared mixins, and every PostgreSQL enum type in `payzeno_ledger`.

The enum value tuples are imported from ``payzeno_contracts.types`` rather than restated
here. That is the point: adding ``variance_exceeded`` to ``RECONCILIATION_ITEM_STATUSES``
in payzeno-contracts and forgetting the ledger is a type error at import, not a runtime
surprise on the settlement path.

Two storage traps are handled once, here (`domain-model.md` §0.1.1):

* ``char(3)`` currency and ``char(2)`` country **blank-pad in Postgres** and compare with
  trailing spaces in some drivers. :class:`TrimmedChar` strips on load, and every
  comparison in this service is therefore against a trimmed literal.
* Money is ``bigint`` and every column name ends ``_minor``. There is no column anywhere
  holding money under a bare name, and no float or numeric money column at all.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar, Final

from payzeno_contracts.types import (
    ACCOUNT_STATUSES,
    ACCOUNT_TYPES,
    ACQUIRERS,
    ENTRY_DIRECTIONS,
    LEDGER_ACTORS,
    LEDGER_PURPOSES,
    LEDGER_REFERENCE_TYPES,
    MATCH_METHODS,
    PAYOUT_FAILURE_CODES,
    PAYOUT_METHODS,
    PAYOUT_STATUSES,
    RECONCILIATION_ITEM_STATUSES,
    RECONCILIATION_LINE_TYPES,
    RECONCILIATION_RUN_STATUSES,
    RECONCILIATION_TRIGGERS,
    SETTLEMENT_BATCH_STATUSES,
)
from sqlalchemy import BigInteger, Boolean, DateTime, MetaData, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import CHAR, TypeDecorator

#: Deterministic constraint and index names, so an Alembic autogenerate diff against a
#: hand-written migration is empty instead of a wall of `ix_None`.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "chk_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

metadata: Final[MetaData] = MetaData(naming_convention=NAMING_CONVENTION)


class TrimmedChar(TypeDecorator[str]):
    """``char(n)`` that strips the blank padding Postgres adds on the way out.

    Without it ``currency == "USD"`` is false for a value stored as ``"USD"`` in a
    ``char(3)`` column read through some drivers, and every currency comparison in the
    reconciliation path silently stops matching.
    """

    impl = CHAR
    cache_ok = True

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        return value.rstrip() if value is not None else None

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        return value.strip() if value is not None else None


#: char(3) ISO-4217, trimmed on load.
Currency = TrimmedChar(3)
#: char(2) ISO-3166-1 alpha-2, trimmed on load.
Country = TrimmedChar(2)
#: char(4) last-four fragments (never a PAN, never a full account number — arc PCI).
LastFour = TrimmedChar(4)
#: char(64) lowercase hex sha256.
Sha256Hex = TrimmedChar(64)

# ---------------------------------------------------------------------------
# PostgreSQL enum types. `create_type=False` everywhere: the migrations own creation,
# and letting the ORM CREATE TYPE on first use races four ECS tasks against each other.
# ---------------------------------------------------------------------------
account_type_enum: Final[PgEnum] = PgEnum(
    *ACCOUNT_TYPES, name="account_type", create_type=False
)
account_status_enum: Final[PgEnum] = PgEnum(
    *ACCOUNT_STATUSES, name="account_status", create_type=False
)
entry_direction_enum: Final[PgEnum] = PgEnum(
    *ENTRY_DIRECTIONS, name="entry_direction", create_type=False
)
ledger_purpose_enum: Final[PgEnum] = PgEnum(
    *LEDGER_PURPOSES, name="ledger_purpose", create_type=False
)
ledger_reference_type_enum: Final[PgEnum] = PgEnum(
    *LEDGER_REFERENCE_TYPES, name="ledger_reference_type", create_type=False
)
ledger_actor_enum: Final[PgEnum] = PgEnum(
    *LEDGER_ACTORS, name="ledger_actor", create_type=False
)
acquirer_enum: Final[PgEnum] = PgEnum(*ACQUIRERS, name="acquirer", create_type=False)
settlement_batch_status_enum: Final[PgEnum] = PgEnum(
    *SETTLEMENT_BATCH_STATUSES, name="settlement_batch_status", create_type=False
)
reconciliation_item_status_enum: Final[PgEnum] = PgEnum(
    *RECONCILIATION_ITEM_STATUSES, name="reconciliation_item_status", create_type=False
)
reconciliation_line_type_enum: Final[PgEnum] = PgEnum(
    *RECONCILIATION_LINE_TYPES, name="reconciliation_line_type", create_type=False
)
reconciliation_match_method_enum: Final[PgEnum] = PgEnum(
    *MATCH_METHODS, name="reconciliation_match_method", create_type=False
)
reconciliation_run_status_enum: Final[PgEnum] = PgEnum(
    *RECONCILIATION_RUN_STATUSES, name="reconciliation_run_status", create_type=False
)
reconciliation_trigger_enum: Final[PgEnum] = PgEnum(
    *RECONCILIATION_TRIGGERS, name="reconciliation_trigger", create_type=False
)
payout_status_enum: Final[PgEnum] = PgEnum(
    *PAYOUT_STATUSES, name="payout_status", create_type=False
)
payout_method_enum: Final[PgEnum] = PgEnum(
    *PAYOUT_METHODS, name="payout_method", create_type=False
)
payout_failure_code_enum: Final[PgEnum] = PgEnum(
    *PAYOUT_FAILURE_CODES, name="payout_failure_code", create_type=False
)
#: `capture_attempt.status` — PAY-2060. Not in payzeno-contracts: it is an internal
#: control table, never serialised onto the bus or an API response.
capture_attempt_status_enum: Final[PgEnum] = PgEnum(
    "pending",
    "captured",
    "failed",
    "indeterminate",
    name="capture_attempt_status",
    create_type=False,
)
#: `funding_event.status` — likewise internal.
funding_event_status_enum: Final[PgEnum] = PgEnum(
    "unmatched",
    "matched",
    "short_paid",
    "disputed",
    name="funding_event_status",
    create_type=False,
)
#: `ledger_adjustment_request.status` — maker-checker states.
ledger_adjustment_status_enum: Final[PgEnum] = PgEnum(
    "pending",
    "approved",
    "rejected",
    "posted",
    name="ledger_adjustment_status",
    create_type=False,
)

ALL_ENUM_TYPES: Final[tuple[PgEnum, ...]] = (
    account_type_enum,
    account_status_enum,
    entry_direction_enum,
    ledger_purpose_enum,
    ledger_reference_type_enum,
    ledger_actor_enum,
    acquirer_enum,
    settlement_batch_status_enum,
    reconciliation_item_status_enum,
    reconciliation_line_type_enum,
    reconciliation_match_method_enum,
    reconciliation_run_status_enum,
    reconciliation_trigger_enum,
    payout_status_enum,
    payout_method_enum,
    payout_failure_code_enum,
    capture_attempt_status_enum,
    funding_event_status_enum,
    ledger_adjustment_status_enum,
)


class Base(DeclarativeBase):
    """Declarative base for every table in ``payzeno_ledger``."""

    metadata = metadata

    #: Set by each concrete model. Used by `BaseRepository` for error details and by the
    #: ops CLI to print a table name without reaching into `__table__`.
    entity_name: ClassVar[str] = "row"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} {identifier}>"


class TimestampMixin:
    """``created_at`` / ``updated_at``, both ``timestamptz not null default now()``.

    ``updated_at`` is maintained by the ``set_updated_at()`` trigger (migration ``0001``),
    not by the ORM: the ops CLI and the Alembic data migrations write rows through raw
    SQL and must not be able to skip it.
    """

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreatedAtMixin:
    """``created_at`` only — for the append-only tables that have no update path."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LivemodeMixin:
    """``livemode boolean not null`` — `domain-model.md` §11.5.

    Test-mode and live-mode money never share a row. It is part of every merchant-scoped
    unique index, ``LedgerPoster.post`` raises :class:`LivemodeMismatchError` when a
    transaction's entries disagree about it, and every consumer copies
    ``EventEnvelope.livemode`` onto the row it writes.

    Added across the board by migration ``0032``, two-phase: backfilled ``true`` from the
    merchant projection, then set ``not null`` one release later.
    """

    livemode: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ProjectionOrderingMixin:
    """``source_event_id`` + ``source_occurred_at`` on every projection table.

    Every projection write is a conditional upsert guarded on ``source_occurred_at``.
    Ordering is not guaranteed on the bus, and ``source_event_id`` is a ULID of the
    *event*, not a monotonic token for the entity — without the timestamp guard a stale
    ``merchant.status_changed`` silently un-restricts a suspended merchant, or re-enables
    ``capture_at_settlement`` in the ledger's view. Migration ``0031``.
    """

    source_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class LegacyIdMixin:
    """A ``bigint`` autoincrement surrogate key.

    Left over from the pre-ULID design (the first two weeks of month 1, before
    `domain-model.md` §0.2 existed). **No model uses it.** It survives because the
    ``0001`` migration's ``down()`` still drops a sequence that this mixin documents the
    existence of, and deleting it every six months and putting it back is worse.
    """

    legacy_id: Mapped[int] = mapped_column(BigInteger, autoincrement=True, nullable=True)
    legacy_source: Mapped[str | None] = mapped_column(String(40), nullable=True)


def money_column(*, nullable: bool = False, default: int | None = 0) -> Mapped[int]:
    """A ``bigint`` money column. Every one of them ends ``_minor`` by convention."""
    return mapped_column(
        BigInteger,
        nullable=nullable,
        server_default=None if default is None else str(default),
    )
