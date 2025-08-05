"""``ledger_transaction`` data access.

Two methods here answer the same question and only one of them is safe, which is the
whole of PAY-2041:

* :meth:`~LedgerTransactionRepository.find_by_idempotency_key` is a plain ``SELECT``. It
  reads committed rows and cannot see a concurrent transaction's uncommitted INSERT, so a
  caller that used it to decide whether to post was doing check-then-act around an
  irreversible side effect. **It is no longer on the money path** — the ops CLI and one
  audit query still call it — and it is kept, with this comment, because deleting it would
  erase the evidence.
