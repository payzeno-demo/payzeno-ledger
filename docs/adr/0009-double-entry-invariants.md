# ADR 0009 — Double-entry invariants, and where they are enforced

- **Status:** accepted
- **Date:** month 2
- **Author:** dhotfix

## Context

A ledger with a single writer and five invariants is a ledger. A ledger with three writers
and a convention is a spreadsheet with a schema.

## Decision

`app/services/transactions.py::LedgerPoster` is the **only** writer of `ledger_transaction`
and `ledger_entry`. Everything else — settlement, payouts, reserves, adjustments,
consumers — goes through it. It enforces, on every post:

1. **Balanced.** Debits equal credits, per currency, within one transaction.
   → `UnbalancedTransactionError`
2. **Positive amounts.** `amount_minor > 0` on every line. Direction lives in `direction`,
   never in the sign — two negative debits net to the right number while every per-account
   aggregate is wrong. → `NegativeAmountError`
3. **One currency.** No transaction spans currencies, and no entry lands on an account of
   a different currency. FX never shipped, so there is nothing legitimate to be tolerant
   of. → `CurrencyMismatchError`
4. **One livemode.** Test-mode and live-mode rows never meet. This is the worst thing the
   service can do quietly: a sandbox charge on a live merchant's balance is a payout of
   real money. → `LivemodeMismatchError`
5. **Account is postable.** `frozen` and `closed` accounts reject. → `AccountFrozenError`,
   409 and not 500, because it is a policy decision and not a fault.

`ledger_entry` is append-only, enforced in the database by `trg_ledger_entry_immutable`
(migration `0004`) and not by convention. Corrections are new transactions —
`ReversalPostingRule`, `AdjustmentPostingRule` — never edits.

