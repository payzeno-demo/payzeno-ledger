"""The repository base class. Layer L3.

**Stateless, session-per-call. This is not a style choice.**

``ReconciliationService.reconcile_batch`` uses three distinct sessions in one pass — a
guard session holding the batch advisory lock, a read session listing items, and a fresh
session per item — against one shared repository instance, and the retry drain uses a
fourth against the same instance. That shared, stateless call site *is* the hazard in arc
INC. A constructor-bound session makes the defect structurally impossible to write, and
then the whole arc is a lie. See `the-incident.md` §3 and `interfaces.md` §3.2.

Every repository is therefore constructed exactly once, in ``app/container.py``, with no
arguments, and every method takes the session it should use as its first parameter.
"""

from __future__ import annotations

import abc
import base64
import binascii
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError, ValidationError

ModelT = TypeVar("ModelT")

#: Nothing may ask for more than this in one page. The console's settlement item table
#: asks for 100; the ops CLI asks for 500 and gets told no.
MAX_PAGE_SIZE: int = 200

#: What `list_page` uses when a caller passes nothing.
DEFAULT_PAGE_SIZE: int = 50


@dataclass(frozen=True, slots=True)
class Page(Generic[ModelT]):
    """One page of results plus the cursor that continues it.

    ``next_cursor`` is ``None`` exactly when ``has_more`` is False, which is what lets a
    caller loop on the cursor alone without also tracking the flag.
    """

    items: list[ModelT]
    next_cursor: str | None
    has_more: bool

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Any:
        return iter(self.items)


def encode_cursor(value: str) -> str:
    """Opaque, URL-safe cursor.

    Base64 rather than the raw id because an exposed primary key in a query string is an
    invitation to hand-edit it, and because a cursor that stops being an id in a later
    release should not be a breaking API change.
    """
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> str:
    """Reverse :func:`encode_cursor`. Raises :class:`ValidationError` on anything else."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValidationError("malformed cursor", details={"cursor": cursor}) from exc


def clamp_limit(limit: int | None) -> int:
    """Keep a caller-supplied page size inside ``[1, MAX_PAGE_SIZE]``."""
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if limit < 1:
        raise ValidationError("limit must be positive", details={"limit": limit})
    return min(limit, MAX_PAGE_SIZE)


class BaseRepository(Generic[ModelT], abc.ABC):
    """Data access for one aggregate.

    Subclasses set :attr:`model` and :attr:`not_found_error` and implement
    :meth:`_default_order`. They add whatever aggregate-specific queries they need; they
    never hold state, never open a transaction, and never commit — the service layer owns
    transactions, and the session it hands down is already inside one.
    """

    #: The declarative class this repository reads and writes.
    model: ClassVar[type[Any]]
    #: What :meth:`get_or_raise` raises. Each concrete repository names its own so the API
    #: error envelope carries `account_not_found` rather than a generic `not_found`.
    not_found_error: ClassVar[type[NotFoundError]] = NotFoundError

    def __init__(self) -> None:
        """Deliberately empty. A repository that holds anything holds too much."""

    @abc.abstractmethod
    def _default_order(self) -> ColumnElement[Any]:
        """The column :meth:`list_page` paginates on, descending."""

    async def get(self, session: AsyncSession, entity_id: str) -> ModelT | None:
        """Fetch one row by primary key, or ``None``.

        A plain ``SELECT``. No ``FOR UPDATE``: a repository does not decide locking, and
        a repository that silently row-locked would have made the sweep and the retry
        agree by accident rather than by design.
        """
        result = await session.get(self.model, entity_id)
        return result  # noqa: RET504  (named for readability in the traceback)

    async def get_or_raise(self, session: AsyncSession, entity_id: str) -> ModelT:
        """Fetch one row by primary key, or raise this repository's not-found error.

        Used everywhere a missing row is a genuine 404 rather than a branch. The service
        layer does not check for ``None`` after calling this — that is the whole point of
        having both methods.
        """
        found = await self.get(session, entity_id)
        if found is None:
            raise self.not_found_error(
                f"{self.model.__name__} not found",
                details={"id": entity_id, "entity": self.model.__name__},
            )
        return found

    async def add(self, session: AsyncSession, obj: ModelT) -> ModelT:
        """Stage one row for insert and flush it.

        The flush is deliberate: it surfaces a unique-index violation here, inside the
        caller's ``try``, instead of at commit time where the caller has already returned
        and the traceback points at the session teardown.
        """
        session.add(obj)
        await session.flush()
        return obj

    async def add_all(self, session: AsyncSession, objs: list[ModelT]) -> list[ModelT]:
        """Stage many rows and flush once.

        ``LedgerPoster`` writes a transaction's entries through this — three to six rows
        per posting, one round trip.
        """
        if not objs:
            return []
        session.add_all(objs)
        await session.flush()
        return objs

    async def list_page(
        self,
        session: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        **filters: Any,
    ) -> Page[ModelT]:
        """Keyset-paginate over :meth:`_default_order`, descending.

        Keyset and not offset: the settlement item tables are millions of rows deep and
        ``OFFSET 40000`` reads forty thousand rows to throw them away. The cursor is the
        last id of the previous page.

        ``filters`` are ANDed equality predicates against model columns. An unknown column
        raises rather than being silently ignored — a typo in a filter name that returns
        every row is how a merchant-scoped list becomes a data leak.
        """
        page_size = clamp_limit(limit)
        order_column = self._default_order()
        stmt = select(self.model)

        for column_name, value in filters.items():
            if value is None:
                continue
            column = getattr(self.model, column_name, None)
            if column is None:
                raise ValidationError(
                    "unknown filter column",
                    details={"column": column_name, "entity": self.model.__name__},
                )
            stmt = stmt.where(column == value)

        if cursor is not None:
            stmt = stmt.where(order_column < decode_cursor(cursor))

        stmt = stmt.order_by(order_column.desc()).limit(page_size + 1)
        rows = list((await session.execute(stmt)).scalars().all())

        has_more = len(rows) > page_size
        items = rows[:page_size]
        next_cursor = (
            encode_cursor(str(getattr(items[-1], order_column.key)))
            if has_more and items
            else None
        )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def count(self, session: AsyncSession, **filters: Any) -> int:
        """Exact count for a filtered set.

        Exact, not estimated: every caller is either an ops screen showing a backlog size
        or an invariant check, and both would rather be slow than wrong.
        """
        from sqlalchemy import func  # noqa: PLC0415  (local: keeps the module import list honest)

        stmt = select(func.count()).select_from(self.model)
        for column_name, value in filters.items():
            if value is None:
                continue
            column = getattr(self.model, column_name, None)
            if column is None:
                raise ValidationError(
                    "unknown filter column",
                    details={"column": column_name, "entity": self.model.__name__},
                )
            stmt = stmt.where(column == value)
        return int((await session.execute(stmt)).scalar_one())

    async def exists(self, session: AsyncSession, entity_id: str) -> bool:
        """Whether a row with this primary key exists.

        Note the shape: this is a read, and using it to decide whether to insert is
        check-then-act, which ADR 0011 forbids in the money path. It is here for the ops
        CLI and for readiness checks, not for the posting path.
        """
        return await self.get(session, entity_id) is not None
