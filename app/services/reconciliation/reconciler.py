"""The batch sweep.

One pass over every eligible item in one settlement batch, serialised against other
sweeps of the same batch by a **batch-scoped Postgres advisory lock** (PAY-1402). The
lock lives in its own guard transaction that outlives every per-item transaction, so a
5,000-item pass never holds 5,000 rows' worth of locks.

Session usage in one pass, deliberately:

* ``guard``  — holds ``pg_advisory_xact_lock(PAY, hash(batch_id))`` for the pass
* ``read``   — lists the eligible items once
