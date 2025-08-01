"""Periodic job tests.

Two things are asserted for every job: that `interval_seconds` reads `app.config.Settings`
(never `os.environ`, which would freeze the value at import and make an incident-night
interval change impossible without a redeploy — see docs/postmortems/2041-duplicate-settlement.md),
and that `run_once` returns a `JobResult` even on the disabled/no-work path.
"""
