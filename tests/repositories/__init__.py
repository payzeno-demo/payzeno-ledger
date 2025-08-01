"""Repository layer tests.

Every module in here is marked ``integration``: repositories emit real SQL (partial
indexes, ``ON CONFLICT``, ``FOR UPDATE SKIP LOCKED``, the append-only trigger) and there
is no honest way to assert that against a stub. They run in the second CI job, after
``pytest -m "not integration"`` is green.

See tests/conftest.py for `pg_engine` (session scoped) and `session` (function scoped).
"""
