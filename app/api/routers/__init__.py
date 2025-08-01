"""The ten routers that make up ``/internal/v1``, plus the two public probes.

One module per group in ``api-surface.md`` §10. :data:`ALL_ROUTERS` is the order
``app/main.py::create_app`` mounts them in, and it is not arbitrary: FastAPI matches
routes in registration order, so ``routers/settlements.py`` — which owns
``/internal/v1/settlement-batches/{batch_id}/items`` — has to be registered before
anything that could claim a broader prefix. Health goes last because it owns no prefix at
all and would otherwise be shadowed by nothing, but keeping it at the end makes the
generated OpenAPI put the operational endpoints where an operator looks for them.

Adding a router here without adding its peer under ``tests/api/`` fails
``tests/api/test_openapi_snapshot.py``, which compares the generated schema against
``contracts/ledger-openapi.json`` — the same file payzeno-api's contract test reads.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import (
    accounts,
    balances,
    entries,
    health,
    invoices,
    ops,
    payouts,
    reconciliation,
    settlements,
    transactions,
)

#: Mount order. Read by ``create_app`` and asserted by ``tests/api/test_routers.py``.
ALL_ROUTERS: tuple[APIRouter, ...] = (
    accounts.router,
    balances.router,
    transactions.router,
    entries.router,
    settlements.router,
    reconciliation.router,
    payouts.router,
    invoices.router,
    ops.router,
    health.router,
)

__all__ = [
    "ALL_ROUTERS",
    "accounts",
    "balances",
    "entries",
    "health",
    "invoices",
    "ops",
    "payouts",
    "reconciliation",
    "settlements",
    "transactions",
]
