"""The HTTP surface. Layer L7.

``payzeno-ledger`` has **no public API**. Everything under this package is mounted at
``/internal/v1``, guarded by :class:`~app.middleware.internal_auth.InternalAuthMiddleware`
and by the ``require_internal_service`` dependency, and the public ingress does not route
to this service at all. The three exemptions — ``/healthz``, ``/readyz``, ``/metrics`` —
are listed in ``api-surface.md`` §10.5 and enumerated in one place,
:data:`app.middleware.internal_auth.PUBLIC_PATHS`.

Three rules this package holds itself to, all of them enforced in review rather than by a
linter:

1. **Handlers are thin.** Open a transaction, call one service method, serialise the
   result. A handler that branches on business state is a rule the workers and consumers
   cannot reach, and every rule in this repo has at least two callers.
2. **No contract type is defined here.** ``app/api/schemas.py`` re-exports
   ``payzeno_contracts.types``; the only local models are the internal request bodies
   with no public equivalent.
3. **One error renderer.** ``app/api/error_handlers.py``, and nothing else, builds an
   ``ApiError``.
"""

from app.api.deps import (
    get_container,
    page_limit,
    require_internal_service,
    require_staff_claim,
)
from app.api.error_handlers import register_error_handlers
from app.api.routers import ALL_ROUTERS

__all__ = [
    "ALL_ROUTERS",
    "get_container",
    "page_limit",
    "register_error_handlers",
    "require_internal_service",
    "require_staff_claim",
]
