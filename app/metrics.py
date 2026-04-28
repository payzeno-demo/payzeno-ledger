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

#: A registry of its own so a test can assert on business metrics without the HTTP
#: middleware's series, and so ``/metrics`` can expose both from one scrape.
BUSINESS_REGISTRY: Final[CollectorRegistry] = CollectorRegistry()

_NAME_RE: Final[re.Pattern[str]] = re.compile(r"(?<!^)(?=[A-Z])")


def _prometheus_name(metric: str) -> str:
    """``SettlementItemPosted`` -> ``payzeno_ledger_settlement_item_posted``.

    Mechanical so the CloudWatch name stays the single source and nobody has to maintain
    a translation table that drifts.
    """
    return "payzeno_ledger_" + _NAME_RE.sub("_", metric).lower()


class Metrics:
    """Lazily-registered counters and histograms, keyed by name and label set.

    Prometheus client objects must be created once per (name, labelnames) pair, and this
    service emits from services, workers, consumers and middleware — none of which knows
    at import time which labels a given call site will pass. So they are built on first
    use and cached. The alternative, declaring every metric up front, was tried: it lasted
    until the third person added a counter and forgot the declaration.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry if registry is not None else BUSINESS_REGISTRY
        self._counters: dict[tuple[str, tuple[str, ...]], Counter] = {}
        self._histograms: dict[tuple[str, tuple[str, ...]], Histogram] = {}

    def increment(self, metric: str, /, **labels: Any) -> None:
        """Add one to ``metric``.

        Label values are stringified rather than validated: a caller that passes an int
        status code should not have to remember to format it, and a caller that passes an
        id will show up in the cardinality dashboard, which is where it gets caught.
        """
        names = tuple(sorted(labels))
        counter = self._counters.get((metric, names))
        if counter is None:
            counter = Counter(
                _prometheus_name(metric),
                f"payzeno-ledger business metric {metric}",
                labelnames=names,
                registry=self._registry,
            )
            self._counters[(metric, names)] = counter
        if names:
            counter.labels(*(str(labels[name]) for name in names)).inc()
        else:
            counter.inc()

    def observe(self, metric: str, value: float, /, **labels: Any) -> None:
        """Record one sample of ``metric``.

        Used for job durations and per-pass counts — ``RetryDrainSettled``,
        ``OutboxBacklog``, ``JobDurationMs``. A pass that settles zero items still
        observes zero; the absence of a sample and a sample of zero mean very different
        things at 02:00 and only one of them is "the drain is running".
        """
        names = tuple(sorted(labels))
        histogram = self._histograms.get((metric, names))
        if histogram is None:
            histogram = Histogram(
                _prometheus_name(metric),
                f"payzeno-ledger business observation {metric}",
                labelnames=names,
                registry=self._registry,
            )
            self._histograms[(metric, names)] = histogram
        if names:
            histogram.labels(*(str(labels[name]) for name in names)).observe(value)
        else:
            histogram.observe(value)


#: The process-wide emitter. Imported as ``from app.metrics import metrics`` by twenty-five
#: modules. A module-level singleton rather than a container dependency on purpose: every
#: layer emits, including L-1 middleware, and threading it through would put a metrics
#: argument on constructors that have no other reason to know about observability.
metrics: Final[Metrics] = Metrics()
