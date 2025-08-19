# ADR 0007 — Strangle payzeno-billing-legacy rather than rewrite it

- **Status:** accepted (in progress, step 4 of 4)
- **Date:** month 1, revised month 8
- **Authors:** nmigration, dhotfix
- **Arc:** MIG

## Context

`payzeno-billing-legacy` is a Spring Boot 2.7 service that owns invoices, subscriptions,
dunning and — historically — fee calculation. It predates payzeno-ledger by two years, it
has 200 files and a jacoco threshold of 55%, and roughly nobody left understands the
