# Runbook — payouts

Owner: Core Payments (inherited from mhandover) · Pager: `#payzeno-alerts`

> **Ownership note.** `app/services/payouts.py`, `app/services/rails/**`,
> `app/models/payout.py` and `app/repositories/payout.py` were mhandover's and have been
> unowned since he left. Three of the four rails have a stubbed submission step (see
> below). Read this before you assume something is broken.

## The four rails

| Method | Cutoff (UTC) | Env | Arrival |
|---|---|---|---|
| `ach` | 21:00 | `PAYOUT_CUTOFF_ACH_UTC` | next business day |
| `same_day_ach` | 16:45 | `PAYOUT_CUTOFF_SAME_DAY_ACH_UTC` | same day, gated by `FLAG_PAYOUT_SAME_DAY_ACH` |
| `sepa` | 14:00 | `PAYOUT_CUTOFF_SEPA_UTC` | next TARGET2 day |
| `faster_payments` | 17:30 | `PAYOUT_CUTOFF_FASTER_PAYMENTS_UTC` | same day |

There is deliberately **no** `PAYOUT_CUTOFF_HOUR_UTC`. Four jurisdictions, four calendars.
If you find code reaching for a single cutoff, that is a bug.

## "The payout says submitted and treasury has no record of it"

Check the rail. `sepa`, `faster_payments` and the debit-ACH puller return a **synthetic**
`rail_reference` and never talk to a bank:

```
# TODO(mhandover): wire the real SFTP drop once treasury signs off
```

Treasury never signed off, mhandover left, and the tickets were reassigned but not done.
Payouts on those rails are ledger-complete and rail-incomplete. `ach` is the only rail with
a real submission path. This is known; do not open a new incident for it. Confirm the
`rail_reference` shape — a synthetic one has no bank prefix — before escalating.

## "Available balance looks wrong"

`PayoutCalculator.compute_available` is `posted − reserved − disputed − in_flight`. The
in-flight subtraction reads `payout` rows a concurrent uncommitted transaction may not have
written yet, which is why `create_payout` takes
`acquire_merchant_currency_lock(merchant_id, currency)` **before** computing anything.

If two payouts exist for one merchant/currency that together exceed available, the advisory
lock was bypassed. `pix_payout_in_flight` is unique since `0033` and should have refused the
second row; if it did not, the index is missing on that environment. Check it before
anything else.

```sql
SELECT merchant_id, currency, count(*), sum(amount_minor)
FROM   payout
WHERE  status IN ('scheduled','submitted','in_transit')
GROUP  BY 1,2 HAVING count(*) > 1;
```

## Reversals

A returned payout posts a `payout_reversal` transaction, never an edit.
`chk_payout_reversal_present` enforces that a `returned`/`failed` payout has a
`reversal_transaction_id`. A row that violates it did not go through `PayoutService`.

## Levers

- `FLAG_PAYOUT_SAME_DAY_ACH=false` — refuses new same-day payouts with
  `payout_blocked`. In-flight ones are unaffected.
- `PayoutSchedulerJob` interval 3600s. Unscheduling it stops payouts advancing; it does not
  stop `POST /internal/v1/payouts`.
