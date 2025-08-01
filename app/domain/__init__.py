"""Pure domain logic — layer L0.

Nothing in this package touches a database session, an HTTP client, a clock or the
environment. It imports ``payzeno_contracts`` and ``app.errors`` and nothing else from
this service (ADR 0002, `docs/adr/0002-layering-and-import-direction.md`). CI holds it to
**100% coverage**; ``app/services/**`` only has to make 85%.

The import order below is load-bearing. ``app.domain.postings`` re-exports the 19 concrete
:class:`~app.domain.postings.PostingRule` classes from ``app.domain.rules.*``, and those
modules import the base class back out of ``postings``. Importing ``postings`` here — at
package-init time, before any ``app.domain.rules.*`` module can be reached directly —
guarantees the base classes exist before the first rule module body runs, whichever
submodule the caller asked for.
"""

from app.domain import backoff, calendar, fees, idempotency, ids, money  # noqa: I001
from app.domain import postings  # noqa: I001  (must follow the modules it builds on)

__all__ = [
    "backoff",
    "calendar",
    "fees",
    "idempotency",
    "ids",
    "money",
    "postings",
]
