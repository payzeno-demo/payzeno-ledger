# PAY-2041 — duplicate settlement postings and 218 double-charged cardholders

- **Severity:** Sev1
- **Detected:** 01:12 UTC, by a merchant emailing support
- **Mitigated:** 01:44 UTC (bleeding stopped) · **Resolved:** 03:20 UTC
- **Author:** apager · **Reviewers:** dhotfix, nmigration, mregression
- **Services:** payzeno-ledger (1.31.1 → 1.31.2 → 1.31.3)
- **Follow-ups:** PAY-2042, PAY-2050, PAY-2053, PAY-2054, PAY-2055, PAY-2056, PAY-2057,
  PAY-2059, PAY-2060

## Summary

Between 00:15 and 00:45 UTC, payzeno-ledger posted **1,847 duplicate settlement
transactions** — two `ledger_transaction` rows with the same `idempotency_key`, six
`ledger_entry` rows where there should have been three. 218 of those items belonged to the
eleven merchants with `capture_at_settlement` enabled, and for each of those we issued a
**second capture against the cardholder's card**. $71,204.18 was refunded the following
morning. Merchant payable balances were overstated by $1.42M for 3h07m, and two settlement
batches sat `partially_reconciled` for six hours.

The cause was two schedulers that had been guarding the same row with two different
mutual-exclusion mechanisms for three months. Neither was wrong on its own. It took a
22-minute acquirer outage to put enough work in front of both of them at once.

## What happened

Two code paths settle a `reconciliation_item`:

- `ReconciliationService.reconcile_batch` — the 15-minute sweep. It takes
  `pg_advisory_xact_lock(PAY, hash(batch_id))` in a guard transaction that outlives the
  whole pass, and takes **no row locks at all**.
- `RetryScheduler.retry_item` — the 60-second drain and the console's retry button. It
  takes `SELECT … FOR UPDATE SKIP LOCKED` on the item row, and took **no advisory lock**.

Each is correct against other copies of itself. The sweep cannot collide with another
sweep; the drain cannot collide with another drain. Neither excludes the other, because
the sweep holds no row lock for `SKIP LOCKED` to skip, and the drain requested no advisory
key for the sweep to contend on.

Both paths then called the same shared `SettlementPoster.post_settlement`, whose
idempotency guard was a `SELECT` on `ledger_transaction.idempotency_key` followed by an
`INSERT` — two statements, at READ COMMITTED, with the cardholder capture issued between
the insert and the commit. The index that `SELECT` relied on,
`ix_ledger_transaction_idempotency_key`, was created **non-unique** by migration `0007`,
so the database had no opinion either.

```
t0  C1 (sweep)  BEGIN; pg_advisory_xact_lock(PAY, hash(sb_B))     -- guard session
t1  C1          BEGIN (item txn); SELECT item ri_X → 'retryable'  -- no row lock taken
t2  C2 (drain)  BEGIN; SELECT item ri_X … FOR UPDATE SKIP LOCKED
                → returned. C1 holds no row lock, so nothing is skipped.
t3  C1          SELECT ledger_transaction WHERE idempotency_key='settle:sb_B:ri_X' → NULL
t4  C2          same SELECT → NULL          -- C1 has not committed
t5  C1          INSERT transaction + 3 entries
t6  C2          INSERT transaction + 3 entries   -- index is NOT unique, no conflict
t7  C1          POST /v2/authorizations/{ntid}/captures  → capture #1
t8  C2          POST /v2/authorizations/{ntid}/captures  → capture #2
t9  C1          UPDATE item SET status='settled' … -- BLOCKS on C2's row lock
t10 C2          UPDATE item SET status='settled', settled_transaction_id='txn_2'
t11 C2          COMMIT                        -- releases the row lock
t12 C1          UPDATE lands, settled_transaction_id='txn_1'; COMMIT  -- last writer wins
```

The row lock **does** serialise the two item updates — but only after both postings and
both captures have already happened. It ordered the damage; it did not prevent it.

## Timeline (UTC)

| Time | Event |
|---|---|
| 23:41 (prev) | Worldflow begins returning `504` on `/captures` **and** `/settlement-files/*/confirm`. 22 minutes. |
| 23:44 | Every item goes `retryable` — `confirm_settlement` is unconditional, so this is not limited to deferred-capture merchants. 4,113 accumulate across `sb_…QK` (USD) and `sb_…7T` (GBP). |
| 00:03 | Worldflow recovers. Backlog 4,113. `RETRY_DRAIN_ENABLED` is true on **one** of four tasks, so the drain does 200/60s — about 21 minutes to clear. |
| 00:15 | `ReconciliationSweepJob` fires into the backlog. First overlap. 611 duplicates. |
| 00:30 | Second sweep. 794 duplicates. |
| 00:45 | Third sweep. 442 duplicates. Total **1,847**. |
| 01:12 | **First signal.** Loomcraft Interiors emails support: "two identical charges, same authorization code, 40 seconds apart". |
| 01:26 | apager paged — by `LedgerBalanceCacheDrift`, an alarm on `merchant_balance_cache` that has nothing to do with the failure. |
| 01:31 | PAY-2041 opened, sev1. |
| 01:38 | nmigration runs the duplicate-key query; 1,847 rows. |
| 01:44 | dhotfix ships one task-definition update that scales to 1 task **and** sets `RETRY_DRAIN_INTERVAL_SECONDS=0`. Bleeding stops. Root cause still unknown. |
| 01:52 | nmigration diffs `reconciler.py` against `retry.py` and sees the lock domains. |
| 02:00 | PR **#171** — `fix/PAY-2043-reconcile-retry-batch-lock`. |
| 02:19 | Merged. 1.31.2 deployed. |
| 02:26 | PR **#172** — unique index + atomic upsert. |
| 02:40 | apager catches that the migration references `settlement_duplicate_audit`, created in a *later* revision in the same PR. Revisions reordered: audit table `0019`, unique index `0020`. |
| 02:52 | mregression posts the lock-convoy objection on #171. Accepted; PAY-2057 filed. |
| 03:05 | #172 merged. `CREATE UNIQUE INDEX CONCURRENTLY` over 41M rows: 14s. |
| 03:20 | Back to 4 tasks, drain interval restored. |
| 09:30 | 218 cardholder refunds initiated (PAY-2042), $71,204.18. |

## What did not detect it

This is the part worth reading twice.

- There was **no alarm on duplicate idempotency keys**. Nothing counted them.
- There was **no alarm on `posted_total_minor > expected_total_minor`**, which was true for
  both batches for six hours.
- The nightly `LedgerAuditJob` trial balance **passed**. A duplicate settlement is
  internally balanced: three debits and three credits, twice. Double-entry integrity is not
  the same property as "we did this once", and our only ledger-level check was the first
  one.
- The alarm that *did* fire, `LedgerBalanceCacheDrift`, is a cache-consistency alarm. It
  pointed at `merchant_balance_cache` and paged the person who owns it. Fourteen minutes
  of the response were spent in the wrong table.
- A merchant found it before we did, 57 minutes in.

## Why it survived three months of review

1. **Each file is individually correct.** You have to hold both open and notice the
   *domains* differ.
2. **The two changes are three months apart** — PAY-1402 (month 5) added the batch lock,
   PAY-1607 (month 6) added the retry. PR #1041's diff never touched `reconciler.py`, so
   no reviewer had it on screen.
3. **The docstring asserted the safety property and was 90% true.** "the `SKIP LOCKED`
   means the drain can't stomp on itself" — correct, and beside the point.
4. **There is an index named after the idempotency key.** Grep for protection, find
   `ix_ledger_transaction_idempotency_key`, stop. You have to open migration `0007` and
   read forty lines in to learn it is `unique=False` and that the comment promises a
   follow-up (PAY-1188) that was never done.
5. **`reconciliation_item.charge_id` has no unique index either** — and correctly cannot,
   because representments legitimately duplicate a charge across batches. So "the table
   doesn't stop it" reads as intentional.
6. **The tests passed and looked thorough.** `test_retry_is_idempotent` calls `retry_item`
   twice and asserts one transaction — sequentially, one event loop, one shared in-process
   session. The second call's `SELECT` sees the first call's row *because they share a
   transaction*. The fixture made concurrency untestable by construction. That is PAY-2053.
7. **The window is normally closed.** In steady state the drain empties `RETRYABLE_STATUSES`
   within ~60s and the sweep fires every 900s; measured on staging the sets overlap on
   0.003% of sweeps. It needs an acquirer outage to become likely.
8. **`AdvisoryLockManager.acquire_item_lock` exists.** Its presence makes the retry path's
   choice look like a considered alternative rather than a gap. Its only caller is
   `PayoutService`.

## The fixes

**PR #171 (02:00) — `_claim_item` takes the batch advisory lock before the row lock.**
Same key the sweep takes. Non-blocking: a retry that loses returns `None`, the route maps
that onto `409 settlement_locked`, and the item stays `retryable` for the next drain — by
which time the sweep has committed `settled` and the status predicate excludes it. This
depends on READ COMMITTED. Nothing in this service sets `isolation_level`; under
REPEATABLE READ the snapshot would be taken at the advisory-lock `SELECT` and the re-read
would still see `retryable`. The fix would look correct and not work.

**PR #172 (02:26) — the guard becomes one statement.** `0019` creates
`settlement_duplicate_audit` and `reverse_duplicate_transactions()`; `0020` quarantines the
1,847 duplicates, posts compensating reversals for them (never `DELETE` — `ledger_entry` is
append-only), and replaces the non-unique index with `uq_ledger_transaction_idempotency_key`
`CONCURRENTLY`. `LedgerPoster.post` gained `on_conflict='return_existing'` and returns
`PostResult(transaction, created)`. `SettlementPoster` publishes
`settlement.duplicate_detected` **unconditionally** when `created` is false, and — the part
that matters to cardholders — **issues no capture on that branch**.

Note that `0020` reverses `txn_2`, the row with the later `posted_at`. That is the one
nothing references: the interleaving above leaves `settled_transaction_id` pointing at
`txn_1` because the sweep's update lands last.

## Action items

| Ticket | Action | Owner | Status |
|---|---|---|---|
| PAY-2042 | Refund the 218 double-charged cardholders | apager | done |
| PAY-2050 | Unique idempotency key + atomic upsert (PR #172) | nmigration | done |
| PAY-2053 | Real-Postgres concurrency fixture; regression test for this race | mregression | done |
| PAY-2054 | Trial balance must also assert one transaction per idempotency key | dhotfix | done |
| PAY-2055 | Alarm on `settlement.duplicate_detected` | apager | done |
| PAY-2056 | Delete the `reconcile_batch_lock_on_retry` kill switch; make the lock unconditional | dhotfix | done |
| PAY-2057 | Drain should skip items whose batch has a running `reconciliation_run`, instead of discovering it lock by lock | — | **open** |
| PAY-2059 | Circuit breaker in front of the acquirer clients; backoff with jitter | apager | done |
| PAY-2060 | `capture_attempt` + `DeferredCaptureJob`; `processor_timeout` moves to `INDETERMINATE_ERROR_CODES` | nmigration | done |

PAY-2057 is the one that is still open. It was filed at 02:52 by dhotfix in response to
mregression's objection on #171: because the batch lock is non-blocking, a drain pass
running during a sweep of the same batch fails to acquire on every one of its 200 attempts
and throughput for that batch goes to zero for the duration. That is the accepted price of
correctness — the sweep is settling the same items anyway — but it means the drain spends
most of every 15-minute window discovering the same fact 200 times. See the `TODO(PAY-2057)`
in `app/services/reconciliation/backlog.py`.

## Lessons

1. **Two mechanisms guarding one row is one mechanism too many.** ADR 0011 now states the
   rule: any path touching a `reconciliation_item` takes the batch advisory lock — in its
   own transaction or in an enclosing guard that outlives it — before any row lock, and
   there is no third mechanism.
2. **A check-then-act guard is a comment, not a constraint.** If the invariant matters, the
   database has to hold it. The index was there; it just was not unique.
3. **Balanced is not the same as correct.** Our only ledger-wide check tested an invariant
   that a duplicate satisfies perfectly.
4. **A test suite that cannot express concurrency will pass on a concurrency bug**, and its
   coverage number will not tell you. `app/services/**` was at 85% and `app/domain/**` at
   100% on the night this happened.
