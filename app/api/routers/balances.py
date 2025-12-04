"""``/internal/v1/balances`` — what a merchant actually has.

Two consumers, and they are not equivalent:

* payzeno-api, behind ``GET /v1/balance`` and the dashboard summary. Latency-sensitive,
  called on every console page load.
* **payzeno-billing-legacy**, ``LedgerClient.getBalance``, from ``StatementController``
  and ``DunningService#shouldRetry``. That one is arc MIG's oldest surviving edge: the
  Java service stopped computing balances in month 1 and has asked us ever since.

``BalanceService`` reads ``merchant_balance_cache`` when the request is for *now* and
falls back to summing ``ledger_entry`` when it is for a point in the past. The route does
not know or care which — that choice is the service's, and it moved once already (arc
PERF, migration ``0015``) without this file changing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import get_balance_service, require_internal_service
from app.api.schemas import Balance, BalanceHistoryResponse
from app.errors import ValidationError
from app.logging import get_logger
from app.services.balances import BalanceService

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/v1/balances",
    tags=["balances"],
    summary="Current or point-in-time balance for one merchant and currency",
)
async def get_balance(
    balances: BalanceServiceDep,
    interval: Annotated[str, Query()] = "day",
    livemode: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Backs the console's balance chart and the legacy statement PDF.

    The window is validated here rather than in the service because it is an HTTP-shaped
    concern: a reversed range is a caller bug, not a ledger condition, and the service is
    also called from the reporting job with a range it built itself.
    """
    if interval not in _INTERVALS:
        raise ValidationError(
            f"interval must be one of {_INTERVALS}", interval=interval
        )
    if to <= from_:
        raise ValidationError(
            "`to` must be after `from`", **{"from": from_.isoformat(), "to": to.isoformat()}
        )
