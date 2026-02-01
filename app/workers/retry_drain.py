"""The 60-second retry drain.

Introduced by PAY-1607 alongside :class:`~app.services.reconciliation.retry.RetryScheduler`
so a transient acquirer failure no longer waits up to fifteen minutes for the next sweep.

Two things about this job are load-bearing and neither is obvious:
