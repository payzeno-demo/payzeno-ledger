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

