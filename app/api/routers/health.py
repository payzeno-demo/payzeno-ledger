"""``/healthz`` and ``/readyz`` — the two public routes on this service.

Public is not an oversight. An ECS health check and payzeno-api's ``/readyz`` probe
cannot present mTLS plus ``X-Payzeno-Service`` plus ``INTERNAL_API_SECRET``, so these two
paths (and the ``/metrics`` mount, which lives in ``app/main.py``) are named explicitly
in :data:`app.middleware.internal_auth.PUBLIC_PATHS`. They carry no merchant data, they
take no parameters, and they are bound to the internal network — the public ingress does
not route to the ledger at all.

The distinction between them is the whole point:

``/healthz``
    Is this process alive? Answers from memory, touches nothing. A failing ``/healthz``
    means the container should be replaced.

``/readyz``
    Should this task receive traffic? Checks the database round-trip. A failing
    ``/readyz`` during a Postgres failover pulls the task out of the load balancer for
    forty seconds instead of restarting it, which is the difference between a blip and a
    rolling restart of the whole service.

Making ``/healthz`` check the database is the classic version of this mistake: every task
fails its liveness probe simultaneously, ECS kills all of them, and the outage outlives
the failover that caused it.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text

from app.api.deps import get_sessions
from app.api.schemas import HealthResponse, ReadinessResponse
from app.logging import get_logger
from app.ports import SessionFactory

logger = get_logger(__name__)

#: No prefix, no auth dependency. These are the three exemptions in `api-surface.md` §10.5.
router = APIRouter(tags=["health"])

SessionsDep = Annotated[SessionFactory, Depends(get_sessions)]

SERVICE_NAME = "payzeno-ledger"

#: Anything slower than this and the database is not usable even if it answered.
READINESS_BUDGET_MS = 1_000

_STARTED_AT = time.monotonic()


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness — is the process running",
)
async def healthz() -> dict[str, Any]:
    """Answers from memory. Never touches the database, deliberately."""
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "uptime_seconds": int(time.monotonic() - _STARTED_AT),
    }


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    summary="Readiness — should this task take traffic",
)
async def readyz(sessions: SessionsDep, response: Response) -> dict[str, Any]:
    """Round-trips ``SELECT 1`` through the pool.

    Uses the same :class:`~app.db.session.PooledSessionFactory` everything else does, so
    an exhausted pool shows up here as a timeout rather than as mysteriously slow
    settlement. ``DATABASE_POOL_SIZE`` is 20 and ``reconcile_batch`` alone holds three
    sessions per concurrent pass; if a readiness probe cannot get a connection, neither
    can the sweep.

    Returns **503** on failure, so the caller does not have to parse the body to route
    on it.
    """
    started = time.perf_counter()
    checks: dict[str, str] = {}
    try:
        async with sessions.begin() as session:
            await session.execute(text("SELECT 1"))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        checks["database"] = "ok" if elapsed_ms <= READINESS_BUDGET_MS else "slow"
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        checks["database"] = "unavailable"
        logger.error(
            "readiness_check_failed",
            error_class=type(exc).__name__,
            error=str(exc)[:200],
            elapsed_ms=elapsed_ms,
        )

    ready = checks["database"] == "ok"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if ready else "degraded",
        "service": SERVICE_NAME,
        "checks": checks,
        "latency_ms": elapsed_ms,
    }
