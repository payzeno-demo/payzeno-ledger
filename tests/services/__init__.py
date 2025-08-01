"""Service layer tests.

These run without Postgres. Repositories are subclassed in tests/services/conftest.py and
their query methods overridden with in-memory equivalents, so the service under test is the
real class, wired to the real ports, with only the SQL replaced.

That substitution is also the reason `tests/services/test_retry.py::test_retry_is_idempotent`
passes on the pre-PAY-2043 code: a shared in-process session cannot express two connections.
The concurrency proof lives in tests/integration/test_reconciliation_concurrency.py.
"""
