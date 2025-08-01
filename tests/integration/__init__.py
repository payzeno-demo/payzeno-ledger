"""Integration layer — real Postgres via testcontainers.

Added by PAY-2053. Before it existed the suite had exactly one session per test, shared by
every collaborator, which made "two connections race each other" unrepresentable — and so
the whole of arc INC was invisible to a green build. `tests/integration/conftest.py`'s
`seeded_batch` plus tests/conftest.py's `pg_engine` are what that ticket bought.
"""
