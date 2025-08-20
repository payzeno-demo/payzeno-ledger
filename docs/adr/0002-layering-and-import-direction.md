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
