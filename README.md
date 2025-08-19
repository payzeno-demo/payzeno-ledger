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

### Tests

```bash
make test              # unit + services + api + repositories + workers. No database.
make test-integration  # testcontainers Postgres. Needs a Docker socket.
make check             # lint + types + test — run this before you open a PR
```

Two coverage gates, both enforced in CI: **100% on `app/domain/`**, **85% on `app/services/`**.
The domain layer is pure functions over frozen dataclasses with no I/O, so 100% is a real bar
and not a vanity number.

---

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

## Gotchas

Read these before your first PR. Most of them are here because of something that already
went wrong.

**Repositories are stateless.** No session on the instance. `reconcile_batch` uses three
sessions in a single pass against one shared repository instance, and the retry drain takes a
fourth. A constructor-bound session would look tidier and would quietly serialise all of it.

**`DATABASE_POOL_SIZE` is not a tuning knob.** It has to stay at or above 3× (concurrent
sweeps + drains). See the sentence above for why.

**Lock ordering is contracted.** Batch advisory lock first, then row lock, always — see
`docs/adr/0011-lock-ordering-in-the-money-path.md`. Two code paths that touch the same
reconciliation item under *different* lock domains is not a style disagreement, it is a
duplicate settlement. That is exactly what PAY-2041 was; the postmortem is in
`docs/postmortems/2041-duplicate-settlement.md` and it is worth twenty minutes.

**Idempotency is a database constraint, not an `if`.** `ledger_transaction.idempotency_key`
has been UNIQUE since migration `0020`. Claim the key inside the transaction
(`claim_idempotency_key`); do not SELECT, check, and then INSERT.
`find_by_idempotency_key` still exists for the CLI and one audit query — it is not on the
money path any more and must not go back on it.

**Publish through the outbox.** `OutboxPublisher` writes in the caller's transaction and rolls
back with it. `SnsPublisher` has exactly one caller (`OutboxDrainJob`). If you publish
directly to SNS from a service you will eventually emit an event for a transaction that never
committed.

**In-process tests cannot see concurrency.** The default `session` fixture is one connection on
one event loop. A test that "proves" two workers cannot collide, written against that fixture,
proves nothing — we have the postmortem to show for it. Anything about racing goes in
`tests/integration/` against the real `pg_engine` fixture, with `@pytest.mark.integration`.

**There are two settlement parsers and that is correct.** Worldflow files are CSV, Nordpay
still files fixed-width. `LegacyFixedWidthParser` is not dead code and there is no plan to
converge them until Nordpay changes their platform.

**`capture_at_settlement` is a column, not a flag.** `SettlementPoster` reads it off the
**charge** row, never the merchant row. The merchant row is a projection and can be newer than
the charge.

**Never log a PAN.** We do not store one — projections hold `account_number_token` and
`*_last_four` and nothing else — and `RedactingFormatter` is a backstop, not a licence.
`FLAG_REDACT_PAN_IN_LOGS` stays on. Compliance signed off assuming it always is.

**Migrations are linear.** One head, ever. If `alembic heads` returns two lines, rebase — do
not create a merge revision. CI checks this, and also checks that your downgrade actually runs.

---

## Contracts and the outside world

Request/response types and event payloads come from **payzeno-contracts** (`payzeno-contracts==3.4.0`,
internal index). `app/api/schemas.py` re-exports them; this repo does not define its own copy
of a shared type, and there is no local `EventEnvelope`.

`contracts/ledger-openapi.json` is a committed snapshot of our OpenAPI document. payzeno-api's
contract test reads it. If you change a route signature, run `make openapi`, commit the diff,
and tell Platform in `#payzeno-platform` — CI will fail their build otherwise and they will
find out from a red pipeline instead of from you.

Events we emit on `payzeno-ledger-events`: `settlement.batch_closed`, `settlement.item_settled`,
`settlement.completed`, `settlement.reconciliation_failed`, `settlement.duplicate_detected`,
`settlement.variance_detected`, `settlement.funded`, `payout.scheduled`, `payout.paid`,
`payout.failed`, `payout.returned`, `ledger.transaction_posted`, `ledger.imbalance_detected`.

Events we consume: `payment.*`, `refund.created`, `dispute.*` on `payzeno-ledger-payments`;
`merchant.*` on `payzeno-ledger-merchants`.

---

