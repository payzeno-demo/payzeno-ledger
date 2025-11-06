"""The application factory.

``create_app()`` takes no arguments. It reads :func:`app.config.get_settings`, builds the
one :class:`~app.container.Container`, installs the six middleware in their contracted
order, mounts the ten routers and the Prometheus scrape endpoint, and registers the single
error handler. Nothing else in this repo builds a ``FastAPI``.

The no-argument signature is deliberate: ``tests/api/test_openapi_snapshot.py`` and
payzeno-api's cross-repo contract test both call ``create_app().openapi()``, and a factory
that needed a settings object would need a fixture to generate a schema.

Lifespan owns two things the ASGI server does not: the APScheduler that runs the eleven
periodic jobs, and the SQS consumer supervisor. Both are started after the container is
built and stopped before the engine is disposed, so a rolling deploy drains in-flight
messages rather than dropping them.

Layering: L8.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from prometheus_client import make_asgi_app

from app.api.error_handlers import register_error_handlers
from app.api.routers import ALL_ROUTERS
from app.config import Settings, get_settings
from app.consumers import register_consumers
from app.container import Container, build_container
from app.logging import configure_logging, get_logger
from app.metrics import BUSINESS_REGISTRY
from app.middleware import install_middleware
from app.workers import register_jobs

logger = get_logger(__name__)

__all__ = ["create_app", "lifespan"]

#: The prefix every business route carries, per api-surface.md §1. Declared on each
#: router rather than applied here; this constant exists so `app/ops/cli.py` and the
#: OpenAPI snapshot test have one spelling of it. `/healthz`, `/readyz` and `/metrics`
#: sit outside it and outside InternalAuthMiddleware — the ECS health check and the
#: Prometheus scrape do not carry the internal secret.
API_PREFIX = "/internal/v1"

DESCRIPTION = """\
Internal double-entry ledger for Payzeno. Settlement import, reconciliation and payouts.

Not public. Every route under `/internal/v1` requires `X-Payzeno-Internal-Secret` and a
client certificate terminated at the internal nginx. Callers are payzeno-api and
payzeno-billing-legacy; this service calls neither of them back.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the scheduler and the consumers; stop them cleanly on shutdown.

    Job registration is *not* fatal on failure of an individual job — see
    ``register_jobs`` — because ten working jobs and one loud error beats a task that will
    not start. Consumer failure is different and does propagate: a ledger task that serves
    HTTP but silently consumes nothing looks healthy and is not.
    """
    container: Container = app.state.container

    scheduler = AsyncIOScheduler(timezone="UTC")
    app.state.scheduler = scheduler
    registered = await register_jobs(scheduler, container)
    scheduler.start()

    supervisor = register_consumers(container)
    app.state.consumers = supervisor
    await supervisor.start()

    logger.info(
        "ledger_started",
        jobs=len(registered),
        retry_drain_enabled=container.settings.retry_drain_enabled,
    )
    try:
        yield
    finally:
        await supervisor.stop()
        scheduler.shutdown(wait=False)
        await container.aclose()
        logger.info("ledger_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    ``settings`` is optional and exists for the integration suite, which needs an app
    pointed at a testcontainer. Production, the OpenAPI snapshot test and payzeno-api's
    contract test all call it with no arguments.
    """
    resolved = settings or get_settings()
    configure_logging(resolved.log_level, json_output=True)

    app = FastAPI(
        title="payzeno-ledger",
        version="1.0.0",
        description=DESCRIPTION,
        docs_url=None,          # internal service; no interactive docs on a money path
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.state.settings = resolved
    app.state.container = build_container(resolved)

    # Order matters and is asserted by tests/api/test_middleware.py. install_middleware
    # adds them bottom-up because Starlette wraps each new one around the stack so far.
    install_middleware(app, resolved, app.state.container.flags)
    register_error_handlers(app)

    for router in ALL_ROUTERS:
        # Each router declares its own full prefix — `/internal/v1/...` — rather than
        # having one bolted on here. A route's path is then greppable from the file that
        # owns it, which is what payzeno-api's contract test needs when it fails and
        # somebody has to find the handler. `health.router` deliberately has no prefix:
        # `/healthz`, `/readyz` are public and exempt from InternalAuthMiddleware.
        app.include_router(router)

    # The scrape endpoint. Mounted rather than routed so it bypasses the router stack —
    # and it exposes BOTH registries: the HTTP histograms from app/middleware/metrics.py
    # on the default registry, and the business counters from app/metrics.py.
    app.mount("/metrics", make_asgi_app(registry=BUSINESS_REGISTRY))

    return app
