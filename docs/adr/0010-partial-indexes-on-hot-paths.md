# ADR 0010 — Partial indexes on the hot paths

- **Status:** accepted
- **Date:** month 7
- **Author:** mregression
- **Arc:** PERF

## Context

`RetryDrainJob` runs every 60 seconds and asks the same question every time: which items
are `retryable` and due. On a `reconciliation_item` table of 34M rows with roughly 400 in
that state, the planner was doing a bitmap heap scan over the status index and filtering by
`next_attempt_at`. p99 for one drain pass: **12 seconds**. A 60-second job spending twelve
of them on one query.

Same shape elsewhere: unfunded batches are a handful of rows in a table of hundreds of
thousands; in-flight payouts are dozens in millions; the active reconciliation run for a
batch is zero or one.

## Decision

Partial indexes, predicated on the state the hot query actually asks about.

| Index | Migration | Predicate |
|---|---|---|
| `pix_reconciliation_item_retryable` | `0014`, re-keyed `0024` | `status IN ('pending','retryable')` |
| `ix_ledger_entry_account_created` | `0015` | — (balance aggregation, not partial) |
| `ix_settlement_batch_currency_date` | `0021` | — |
| `pix_settlement_batch_unfunded` | `0005` | `status = 'reconciled' AND funded_at IS NULL` |
| `pix_payout_in_flight` | `0018`, unique from `0033` | `status IN ('scheduled','submitted','in_transit')` |
| `pix_reconciliation_run_active` | `0008` | `status = 'running'` |
| `pix_capture_attempt_pending` | `0025` | `status = 'pending'` |

Drain p99 went from 12s to **40ms**.

## Consequences

The index only helps while the predicate matches the query. `0024` re-keyed
`pix_reconciliation_item_retryable` to `(next_attempt_at, id)` when `next_attempt_at` was
added by PAY-2059, because the original key stopped covering the ordering and the planner
quietly went back to a heap scan. Nobody noticed for two days. If you change what a hot
query filters on, the partial index is part of the change.

**And the honest consequence, in hindsight.** Making the drain fast is what made PAY-2041
possible. Before `0014` the drain could not keep up with a 4,113-item backlog well enough
to still be working when the next 15-minute sweep landed; after it, the drain and the sweep
were in the same rows at the same time. The index did not cause the bug — two lock domains
did — but it is what closed the distance between them. That is in the postmortem and it is
here too, because a performance change that alters concurrency is a concurrency change.
