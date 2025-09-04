"""The 900-second batch sweep.

Registered in every ledger task. Production runs four tasks, so there are four
unsynchronised sweeps, and the batch advisory lock inside
``ReconciliationService.reconcile_batch`` is what keeps them from colliding with each
other.
