"""Business metrics — the named counters and gauges the alarms are built on.

Two metric surfaces exist in this service and they are not the same thing:

* ``app/middleware/metrics.py`` owns the **HTTP** histograms — requests, latency,
  in-flight. Route templates, nothing per-request.
* this module owns the **business** counters — ``SettlementItemPosted``,
  ``DuplicateSettlementDetected``, ``JobDurationMs``. These are what
  payzeno-infrastructure's CloudWatch alarms key on, and their names are copied into
  ``modules/monitoring/alarms.tf``. **Renaming one here silently disables an alarm.**

The names are PascalCase, which is not this codebase's convention anywhere else. They are
CloudWatch metric names first and Prometheus series second — the ``Payzeno/Ledger``
namespace predates the Prometheus scrape by about a year, the alarms were never renamed,
and having two names for one number is worse than one ugly one.

Cardinality: label values must be closed sets — an acquirer, a caller, a job name, a
status code. Never an id. ``DuplicateSettlementDetected`` labelled by ``item_id`` would
have created 1,847 series on the night of PAY-2041.

Layering: L-1.
"""

from __future__ import annotations

import re
from typing import Any, Final

from prometheus_client import CollectorRegistry, Counter, Histogram

__all__ = ["BUSINESS_REGISTRY", "Metrics", "metrics"]

def _prometheus_name(metric: str) -> str:
    """``SettlementItemPosted`` -> ``payzeno_ledger_settlement_item_posted``.

    Mechanical so the CloudWatch name stays the single source and nobody has to maintain
    a translation table that drifts.
    """
    return "payzeno_ledger_" + _NAME_RE.sub("_", metric).lower()


#: The process-wide emitter. Imported as ``from app.metrics import metrics`` by twenty-five
#: modules. A module-level singleton rather than a container dependency on purpose: every
#: layer emits, including L-1 middleware, and threading it through would put a metrics
#: argument on constructors that have no other reason to know about observability.
metrics: Final[Metrics] = Metrics()
