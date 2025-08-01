"""payzeno-ledger — the double-entry ledger behind Payzeno.

Owns four things and nothing else:

1. the ledger itself — ``ledger_transaction`` and its append-only ``ledger_entry`` rows,
   written through exactly one class, ``app/services/transactions.py::LedgerPoster``;
2. settlement import — acquirer files in, ``settlement_batch`` and ``reconciliation_item``
   rows out;
3. reconciliation — matching those items to charge projections and posting them;
4. payouts — four rails, four cutoffs, four calendars.

It is an **internal** service. Everything under ``/internal/v1`` is reached by payzeno-api
and payzeno-billing-legacy; the arrows point inward only and this service makes no HTTP
call back to either of them. Its outbound calls go to the two acquirers, and its outbound
events go onto ``payzeno-ledger-events`` through the transactional outbox.

Import direction is strictly downward (ADR 0002). ``app/domain/**`` imports nothing from
``app/`` except ``app.errors``; ``app/errors.py``, ``app/ports.py``, ``app/config.py``,
``app/clock.py``, ``app/flags.py``, ``app/logging.py`` and ``app/metrics.py`` sit below
everything and import nothing from ``app/`` but each other.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Kept in step with ``pyproject.toml``'s ``project.version``. Stamped onto the OpenAPI
#: document and reported by ``GET /healthz``, which is how a deploy is confirmed and how
#: "1.31.1 vs 1.31.2" in the PAY-2041 postmortem means anything.
__version__ = "1.0.0"
