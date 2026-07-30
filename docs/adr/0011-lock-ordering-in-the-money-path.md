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

## Why this is sufficient, stated precisely

Read this carefully, because the obvious version of the argument is wrong.

The sweep holds the batch advisory lock in a long-lived **guard** transaction that outlives
every per-item transaction, and it takes no row locks at all. The retry takes the advisory
lock and then the row lock, in one transaction, advisory first. No transaction acquires a
row lock before an advisory lock, and none holds a row lock while waiting on an advisory
lock, so no cycle is possible.

Note the shape of that. It is **not** "acquisition order is globally advisory-then-row on
both paths" — on the sweep side the two locks are never held by one transaction at all, and
that split is precisely the loophole that created PAY-2041. The fix works for a different
reason: the retry's advisory acquisition contends with the *guard* transaction, which
outlives every item transaction. A reviewer who cites the naive version is citing a rule the
sweep itself violates.

**This depends on READ COMMITTED.** Nothing in payzeno-ledger sets `isolation_level` and
nothing may. Under REPEATABLE READ the snapshot is taken at the transaction's first
statement — which after the fix is the advisory-lock `SELECT` — so a retry that queued
behind the sweep would re-read `status = 'retryable'` from its own stale snapshot and post a
duplicate anyway. The fix would look correct and not work. `app/db/session.py`'s docstring
says this; `tests/conftest.py`'s `pg_engine` says it again.

## Consequences

The batch lock on the retry path is **non-blocking** (`pg_try_advisory_xact_lock`). A sweep
can hold the lock for minutes across 5,000 items, and `pg_advisory_xact_lock` would park a
drain worker on a pooled connection for the whole pass — 200 of those exhausts
`DATABASE_POOL_SIZE`. Failing fast leaves the item `retryable` for the next drain, which is
what we want, because the sweep is settling it anyway.

The price is a lock convoy, and mregression named it on PR #171 before it merged: during a
sweep, every attempt in a drain pass fails to acquire and throughput for that batch goes to
zero. Accepted, because retry latency is then bounded by the sweep rather than by the drain
interval. **PAY-2057** is the real fix — skip items whose batch has a `running`
`reconciliation_run` instead of discovering it lock by lock — and it is not done.

A lock is not the primary defence and must not be the only one. `0020` made
`ledger_transaction.idempotency_key` unique and `LedgerPoster` claims it with
`ON CONFLICT DO NOTHING RETURNING`. If both locks were removed tomorrow the database would
still refuse the second row. That is the order to think in: constraint first, lock second,
comment never.
