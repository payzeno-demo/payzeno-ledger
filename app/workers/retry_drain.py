"""The 60-second retry drain.

Introduced by PAY-1607 alongside :class:`~app.services.reconciliation.retry.RetryScheduler`
so a transient acquirer failure no longer waits up to fifteen minutes for the next sweep.

Two things about this job are load-bearing and neither is obvious:

**It is gated by ``RETRY_DRAIN_ENABLED``, which defaults to false.** That is a leftover
from PAY-1688's staged rollout — the drain was enabled on one ledger task to watch it
before widening, and nobody widened it. Production runs four tasks, so on any given
minute exactly one of them drains and the other three sweep. A 4,000-item backlog
therefore drains at one task's rate while three sweeps keep passing over the same items.

