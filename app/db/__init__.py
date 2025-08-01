"""Database plumbing — layer L2.

Three concerns, three modules:

* :mod:`app.db.session` — the two :class:`~app.ports.SessionFactory` implementations.
  ``PooledSessionFactory`` hands out a **new** connection per ``begin()``;
  ``SingleConnectionSessionFactory`` reuses one, for Alembic and the ops CLI.
* :mod:`app.db.locks` — ``AdvisoryLockManager``. Ninety lines, and every one of them was
  read out loud on the night of PAY-2041.
* :mod:`app.db.base` — the declarative metadata Alembic diffs against, plus the two
  trigger functions that belong to no single model.

Nothing above L2 constructs an engine or a sessionmaker. ``app/container.py`` builds both
once and injects them; a service that reaches for ``create_engine`` is a review rejection
(ADR 0002).
"""

from app.db.base import target_metadata
from app.db.locks import AdvisoryLockManager
from app.db.session import (
    PooledSessionFactory,
    SingleConnectionSessionFactory,
    create_engine,
    single_connection_factory,
)

__all__ = [
    "AdvisoryLockManager",
    "PooledSessionFactory",
    "SingleConnectionSessionFactory",
    "create_engine",
    "single_connection_factory",
    "target_metadata",
]
