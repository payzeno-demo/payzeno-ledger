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

| Step | Capability | Landing zone | Status |
|---|---|---|---|
| 1 | fee computation | `app/domain/fees.py` | **done**, month 4 |
| 2 | merchant balance | `app/services/balances.py`, `routers/balances.py` | **done**, month 5 |
| 3 | payouts | `app/services/payouts.py`, `app/services/rails/**` | **done**, month 7 |
| 4 | invoice **lines** | `app/services/invoices.py`, `routers/invoices.py`, `invoice_line_staging` (`0022`) | **in flight** |

Step 1 left a scar on purpose. `app/domain/fees.py::legacy_blended_fee` is kept bit-for-bit
compatible with Java's `LegacyBlendedFeeCalculator` so the parity tests can run both against
the same fixtures. It was marked deprecated in month 4 and is still reached by one branch of
`apportion_fee`, because the merchants on the old blended pricing model have not been
migrated and nobody has scheduled it.

## The open disagreement

Step 4 moves invoice *lines*. It does **not** move invoice *numbering*, and that is the
argument. nmigration's position is that a ledger that stages lines but cannot issue a number
is a half-migration that will sit there for a year; dhotfix's is that invoice numbering is a
statutory sequence per jurisdiction, that `SequentialInvoiceNumberGenerator` has a
per-country ruleset nobody has written down, and that the ledger has no business owning a
legal artifact it cannot validate.

It is unresolved. `migrations/0022` and `app/models/invoice_line_staging.py` exist, the
