"""Per-item retry (PAY-1607).

Before this existed, an item that failed a settlement attempt waited up to fifteen
minutes for the next batch sweep. Two entry points share this class: the internal
``POST /internal/v1/reconciliation/items/{itemId}/retry`` route, which the admin console
reaches through payzeno-api, and ``RetryDrainJob``, which drains the retryable backlog
every sixty seconds.
"""

from __future__ import annotations

