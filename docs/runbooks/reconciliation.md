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

