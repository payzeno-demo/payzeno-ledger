# ADR 0002 — Layering and import direction

- **Status:** accepted
- **Date:** month 1
- **Author:** dhotfix
- **Supersedes:** —

## Context

The first two weeks produced a `services/` package that imported `api/` for its request
models and an `api/` package that imported `services/` for everything else. It worked. It
also meant no module could be read without reading its callers, and the third person to
join could not answer "what does this file depend on" for any file.

## Decision

Imports flow **strictly downward**. A module may import from its own layer's siblings
only where the layer's own docstring says so. An upward import is a review rejection.

```
L8   app/main.py, app/container.py
L7   app/api/**                      (app/middleware/** sits beside it)
L6   app/workers/**, app/consumers/**  (app/ops/** sits beside it)
L5   app/services/**
L4   app/clients/**, app/publishers/**
L3   app/repositories/**
L2   app/db/session.py, app/db/locks.py
L1   app/models/**
L0   app/domain/**
L-1  app/ports.py  app/errors.py  app/config.py  app/flags.py
     app/clock.py  app/logging.py  app/metrics.py
```

L-1 is importable from everywhere and imports nothing from `app/` but itself.
`app/domain/**` imports `app.errors` and `payzeno_contracts` and **nothing else** — no
session, no model, no repository. `app/models/**` imports `app/models/base.py` and the
enum tuples from `payzeno_contracts.types`.

Two consequences people trip over, both intentional:

- `app/api/deps.py` needs `Container` for its type hints and `Container` is L8. It imports
  it under `TYPE_CHECKING` only.
- `app/domain/calendar.py` cannot import `app/models/reserve.py`, so
  `BankingCalendar.from_rows` duck-types whatever it is handed. That is not laziness; it is
  the layering rule with its consequence paid rather than avoided.

## Consequences

Good: any file in `app/domain/` can be read, tested and reasoned about with no database
and no framework. CI enforces 100% coverage there because there is nothing to mock.

Bad: `app/container.py` is enormous and it is hot — every new service and every new
dependency touches it. That is the cost of having exactly one construction site, and it is
a cost worth paying: see ADR 0011 and the postmortem for what the alternative bought us.
