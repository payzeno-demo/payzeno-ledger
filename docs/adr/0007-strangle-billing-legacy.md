# ADR 0007 — Strangle payzeno-billing-legacy rather than rewrite it

- **Status:** accepted (in progress, step 4 of 4)
- **Date:** month 1, revised month 8
- **Authors:** nmigration, dhotfix
- **Arc:** MIG

## Context

`payzeno-billing-legacy` is a Spring Boot 2.7 service that owns invoices, subscriptions,
dunning and — historically — fee calculation. It predates payzeno-ledger by two years, it
has 200 files and a jacoco threshold of 55%, and roughly nobody left understands the
dunning scheduler.

The two options were a rewrite behind a flag, and a strangler: move one capability at a
time into payzeno-ledger, keep the Java service calling us for the moved part, and delete
the Java code only once the ledger has been authoritative for a full billing cycle.

## Decision

Strangler. Four steps, each behind a flag on both sides
(`FLAG_LEDGER_OWNS_*` / `BILLING_FLAG_LEDGER_OWNS_*`):

