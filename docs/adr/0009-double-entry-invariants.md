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
