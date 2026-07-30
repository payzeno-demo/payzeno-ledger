# Runbook — reconciliation

Owner: Core Payments · Pager: `#payzeno-alerts` · Service: payzeno-ledger

## What this system is

Two schedulers settle `reconciliation_item` rows, and you need to know which one you are
looking at.

| | `ReconciliationSweepJob` | `RetryDrainJob` |
|---|---|---|
| interval | 900s (`RECONCILE_SWEEP_INTERVAL_SECONDS`) | 60s (`RETRY_DRAIN_INTERVAL_SECONDS`) |
| enabled | always, every task | `RETRY_DRAIN_ENABLED`, **per task** |
| entry point | `ReconciliationService.reconcile_batch` | `RetryScheduler.drain` → `retry_item` |
| serialises on | batch advisory lock, held in a guard txn for the pass | batch advisory lock (non-blocking) **then** `FOR UPDATE SKIP LOCKED` on the item |
| batch size | `RECONCILE_MAX_ITEMS_PER_RUN` (500) | `RETRY_DRAIN_BATCH_SIZE` (50) |

Both call the same `SettlementPoster`. Both can be running at once. That is expected and
safe; it was not always (see `docs/postmortems/2041-duplicate-settlement.md`).

## Alarms and what they mean

| Alarm | Metric | First move |
|---|---|---|
| `DuplicateSettlementDetected` | `Payzeno/Ledger DuplicateSettlementDetected` | **Not an outage.** Since PAY-2043 a retry losing a race to a sweep is the normal path and it returns `409 settlement_locked` without publishing. A *duplicate_detected* means two callers both reached the claim. Run the duplicate query below. If it returns rows, escalate — the unique index should have made this impossible. |
| `SettlementBacklogHigh` | retryable item count | `GET /internal/v1/reconciliation/backlog`. Check the acquirer first, not us. |
| `ReconciliationRunStuck` | run in `running` > 30m | "Who holds the lock", below. |
| `LedgerBalanceCacheDrift` | `merchant_balance_cache` vs `ledger_entry` | This alarm is about the **cache**, not the ledger. It is the alarm that paged the wrong person on PAY-2041 night. Confirm with the trial balance before you touch the cache. |

## The duplicate query

The one that found 1,847 rows at 01:38.

```sql
SELECT idempotency_key,
       count(*)                       AS rows,
       min(posted_at)                 AS first_posted,
       max(posted_at)                 AS last_posted,
       array_agg(id ORDER BY posted_at) AS transaction_ids
FROM   ledger_transaction
WHERE  purpose = 'settle'
  AND  posted_at > now() - interval '24 hours'
GROUP  BY idempotency_key
HAVING count(*) > 1
ORDER  BY count(*) DESC;
```

Since migration `0020` this cannot return rows —
`uq_ledger_transaction_idempotency_key` is unique. If it does, stop and page. Do **not**
`DELETE`: `ledger_entry` is append-only and enforced by trigger. The correction path is
`SELECT reverse_duplicate_transactions('<idempotency_key>')`, which quarantines into
`settlement_duplicate_audit` and posts a compensating `reversal`. It reverses the row with
the later `posted_at`, which is the one `reconciliation_item.settled_transaction_id` does
not reference.

And check for cardholder impact separately — the ledger row is the cheap half:

```sql
SELECT c.charge_id, c.merchant_id, count(*) AS captures
FROM   capture_attempt a
JOIN   settlement_charge c ON c.charge_id = a.charge_id
WHERE  a.created_at > now() - interval '24 hours'
GROUP  BY 1, 2
HAVING count(*) > 1;
```

## Who holds the lock

`AdvisoryLockManager` keys are `blake2b(batch_id, digest_size=4)` XOR `0x504159`, signed.
You cannot compute it in your head. Get it from the service:

```python
from app.db.locks import AdvisoryLockManager
AdvisoryLockManager().batch_key("sb_...")     # -> the int4 in pg_locks.objid
```

then:

```sql
SELECT l.pid, l.objid, a.state, a.query_start, now() - a.query_start AS held_for,
       left(a.query, 120) AS query
FROM   pg_locks l
JOIN   pg_stat_activity a USING (pid)
WHERE  l.locktype = 'advisory'
  AND  l.classid  = 5259353          -- 0x504159, the PAY namespace
ORDER  BY a.query_start;
```

A sweep holding the key for more than `RECONCILE_SWEEP_WALL_BUDGET_SECONDS` (30) is a bug —
the guard transaction is supposed to commit and reopen at that cadence. A sweep holding it
for minutes means the budget is not being honoured; capture the query and open a ticket.

## Drain throughput is zero and the backlog is not moving

Almost always the lock convoy, and it is expected behaviour, not a fault. While a sweep of
batch `B` is running, every `_claim_item` in a drain pass requests the same batch key,
fails non-blockingly, and returns `None`. Throughput for `B` is zero until the sweep
finishes. The items are being settled — by the sweep.

Confirm: `checked_out` on the pool climbing toward `pool_size` while a run is `running` for
that batch. `GET /readyz` reports the pool gauges.

This is **PAY-2057**, filed at 02:52 on the incident night and still open. Do not "fix" it
by making the retry lock blocking: 200 drain attempts parked on pooled connections
exhausts `DATABASE_POOL_SIZE` and takes the HTTP surface down with it.

## Levers, in order of preference

1. `RETRY_DRAIN_ENABLED=false` — stops the drain on a task. Read per tick, so it takes
   effect within 60s, no redeploy.
2. `RECONCILE_MAX_ITEMS_PER_RUN` — shortens a sweep pass, so the batch lock is released
   sooner.
3. `RECONCILE_MAX_ATTEMPTS` — read off `Settings` at call time. Lowering it stops items
   cycling; they land in `failed` and need a manual match.
4. `RETRY_DRAIN_INTERVAL_SECONDS=0` — unschedules the job entirely. Needs a task restart,
   because `register_jobs` reads the interval once. This is the heavy hammer used at 01:44
   on PAY-2041 night, and it is why the same change also scaled the service to one task.
