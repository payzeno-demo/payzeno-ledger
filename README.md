# payzeno-ledger

The double-entry ledger. Every cent Payzeno moves is a balanced transaction in this service:
authorizations, captures, fees, settlements, disputes, reserves, payouts, adjustments.
If it does not balance, it does not get written.

Owned by **Core Payments**. Two approvals to merge, and that is not negotiable on anything
under `app/domain/`, `app/services/transactions.py`, or `migrations/`.

- **Python 3.12**, FastAPI 0.111, SQLAlchemy 2.0 (async), Alembic, APScheduler
- **Port 8000**, Postgres 15 (`payzeno_ledger`), SQS in / SNS out
- Internal only. `/internal/v1/**` is unreachable from the public listener — nginx denies
  `^/internal/` and the ALB carries the same rule. The only callers are **payzeno-api** and
  **payzeno-billing-legacy**.

---

## What it actually does

Four things, in order of how much of the code they are:

1. **Posts transactions.** `LedgerPoster.post` is the single writer of `ledger_transaction`
   and `ledger_entry`. Everything else — consumers, settlement, payouts, adjustments — goes
   through it. It enforces five invariants (balanced, single currency, non-negative amounts,
   livemode consistency, no posting to a frozen account) and refuses the write otherwise.
2. **Imports and reconciles settlement.** We pull settlement files from Worldflow and Nordpay,
   parse them, match each line to a charge projection, and post the resulting entries. The
   unmatched remainder is the reconciliation backlog.
3. **Pays merchants out.** Available balance, four rails (ACH, same-day ACH, SEPA, Faster
   Payments), four cutoffs, four calendars.
4. **Audits itself.** A nightly trial balance per currency. If it fails, it raises
   `LedgerIntegrityError` and publishes `ledger.imbalance_detected`, and somebody's night ends.

It does **not** own merchants, charges, or bank accounts. Those are projections, built from
events payzeno-api emits. We never call payzeno-api. The arrows point inward.

---

## Running it

```bash
cp .env.example .env
make install          # uv sync --locked --all-extras
make migrate          # alembic upgrade head
make run              # uvicorn on :8000
```

That gives you the HTTP surface only. The periodic jobs and the SQS consumers are separate
processes on purpose — same image, different command:

```bash
make worker           # APScheduler: the 11 jobs below
make consumer         # the two SQS consumers
```

For the whole stack (Postgres, localstack, the acquirer sandbox):

```bash
make up               # compose, wired to ../payzeno-infrastructure/docker-compose.yml
```

You need `payzeno-infrastructure` checked out as a sibling directory. Every compose service
lives in the root file there; `docker-compose.override.yml` here only adds the bind mount and
the reload flag.

## The periodic jobs

All eleven subclass `PeriodicJob` and register in `app/workers/__init__.py::register_jobs()`.
Intervals come off `Settings` — read at construction, never at import — which is why they can
be changed with a task restart instead of a redeploy. That mattered once.

| Job | Every | What it does |
|---|---|---|
| `ReconciliationSweepJob` | 900s | walks open batches, reconciles items under a batch advisory lock |
| `RetryDrainJob` | 60s | drains retryable items. **Gated by `RETRY_DRAIN_ENABLED`** |
| `SettlementImportJob` | 1h | fetches yesterday's settlement files from both acquirers |
| `FundingMatchJob` | 900s | matches bank funding events to closed batches |
| `DeferredCaptureJob` | 30s | resolves indeterminate captures via `get_capture_status` |
| `PayoutSchedulerJob` | 1h | schedules payouts due before the next per-rail cutoff |
| `ReserveReleaseJob` | daily | releases reserve holds whose window has elapsed |
| `NegativeBalanceJob` | daily | flags merchants whose available balance has gone negative |
| `LedgerAuditJob` | daily | trial balance per currency |
| `OutboxDrainJob` | 5s | ships the transactional outbox to SNS |
| `BatchCloseJob` | 1h | closes batches whose items are all terminal |

---

