# ADR 0011 — Lock ordering in the money path

- **Status:** accepted
- **Date:** month 9, three days after PAY-2041
- **Author:** dhotfix
- **See also:** `docs/postmortems/2041-duplicate-settlement.md`

## Context

We had two mutual-exclusion mechanisms guarding one row, in two files written three months
apart, and neither excluded the other.

`ReconciliationService.reconcile_batch` serialised on
`pg_advisory_xact_lock(PAY, hash(batch_id))` held in a guard transaction, and took no row
locks. `RetryScheduler._claim_item` serialised on `SELECT … FOR UPDATE SKIP LOCKED` on the
item, and took no advisory lock. Each was correct against copies of itself. `SKIP LOCKED`
skips nothing when the other party holds no row lock, and an advisory key nobody else
requests excludes nobody. It took an acquirer outage to make the two paths meet, and when
they did we posted 1,847 duplicate transactions and double-charged 218 cardholders.

## Decision

> **Any code path that touches a `reconciliation_item` acquires the batch advisory lock —
> in its own transaction, or in an enclosing guard transaction that outlives it — before it
> takes any row lock. No transaction holds a row lock while waiting on an advisory lock.
> There is no third mechanism.**

The same shape applies on the payout path: `PayoutService.create_payout` takes
`acquire_merchant_currency_lock` **before** computing available balance, because
`compute_available` subtracts in-flight payouts by reading rows a concurrent uncommitted
transaction has not written yet — the identical check-then-act, on the path that moves money
to a bank account. `pix_payout_in_flight` (unique since `0033`) is the database's last word
if the lock is ever bypassed.

`AdvisoryLockManager` is the only place advisory locks are taken. Its docstring lists every
caller of every method, exhaustively, and that list is part of the contract: if you add a
caller, you add it there.

