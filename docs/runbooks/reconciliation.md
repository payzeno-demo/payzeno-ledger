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

