"""Fixtures for the router layer.

The routers in ``app/api/routers/`` are thin by contract: they validate HTTP-shaped
inputs, call exactly one service or repository, and shape the response. That is what
makes them testable as plain coroutines — every handler here is called directly, with its
dependencies passed positionally, rather than through ``TestClient``. A ``TestClient``
round trip would also exercise FastAPI's own dependency resolution, which is not the thing
under test and which drags ``app/container.py`` (and therefore a database URL) into a unit
test.

What the routers *do* need is a :class:`~app.ports.SessionFactory`, because
``async with sessions.begin() as session`` is the one piece of infrastructure a handler
touches directly. :class:`RouteSessionFactory` is that, and — unlike
``tests/services/conftest.py``'s ``SharedSessionFactory`` — it hands out a **new** session
per ``begin()``. Routes never share a session with anything; modelling them as if they did
would hide a handler that leaks one transaction into another.

`app/api/deps.py::require_internal_service` is not stubbed here. It is a synchronous
function over a ``Request`` and ``tests/api/test_deps.py`` calls it with real request
objects, which is more honest than a fixture that always says yes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import pytest

from app.ports import SessionFactory

#: Every route test that renders a timestamp renders this one.
NOW = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)


class FakeSession:
    """Stands in for ``AsyncSession`` at the router boundary.

    Handlers pass this straight down to a stubbed service or repository, so the only
    methods that ever fire are the ones a handler itself calls: ``execute`` (``/readyz``
    round-trips ``SELECT 1``) and the transaction verbs the context manager drives.
    """

    def __init__(self, index: int) -> None:
        self.index = index
        self.executed: list[str] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.executed.append(str(statement))
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    def __repr__(self) -> str:
        return f"<FakeSession {self.index}>"


class RouteSessionFactory(SessionFactory):
    """A ``SessionFactory`` that opens a fresh session per ``begin()``.

    The explicit base is the ``IMPLEMENTS`` edge — ``SessionFactory`` is a ``Protocol`` and
    Python would have inferred it, but an inferred relationship is invisible to anything
    reading the source.

    ``begin()`` commits on clean exit and rolls back on an exception, which is exactly what
    ``PooledSessionFactory`` does. Route tests assert on ``rolled_back`` to prove that a
    handler which raises a :class:`~app.errors.PayzenoLedgerError` does not leave a
    half-written transaction behind for ``error_handlers.py`` to inherit.
    """

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.begin_count = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[FakeSession]:
        session = FakeSession(self.begin_count)
        self.begin_count += 1
        self.sessions.append(session)
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()

    @property
    def last(self) -> FakeSession:
        """The most recently opened session. Convenience for single-``begin()`` handlers."""
        if not self.sessions:
            raise AssertionError("no session was ever opened")
        return self.sessions[-1]


class FailingSessionFactory(RouteSessionFactory):
    """``begin()`` raises. Used by ``/readyz`` to model a database that is not there."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error or OSError("connection refused")

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[FakeSession]:
        self.begin_count += 1
        raise self.error
        yield FakeSession(-1)  # pragma: no cover - unreachable, keeps the type a generator


class Body:
    """Stands in for a parsed pydantic request model.

    Handlers only ever read attributes off their body argument, so an object with the right
    attributes is indistinguishable from the real schema — and building the real schema
    would couple every route test to ``app/api/schemas.py``'s re-exports from
    ``payzeno_contracts``, which change on a different repo's release cadence.
    """

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


@pytest.fixture
def sessions_factory() -> RouteSessionFactory:
    return RouteSessionFactory()


@pytest.fixture
def failing_sessions() -> FailingSessionFactory:
    return FailingSessionFactory()
