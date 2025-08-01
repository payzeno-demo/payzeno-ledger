"""The payzeno-ledger test suite, in seven layers.

======================  =====================================================
Layer                    What it proves
======================  =====================================================
``tests/unit/``          pure logic — money, fees, postings, backoff, locks,
                         the error table. 100% coverage is a CI gate here.
``tests/repositories/``  real SQL against real Postgres, one file per aggregate
``tests/services/``      business logic against in-memory repositories
``tests/consumers/``     SQS handlers and the insert-first dedupe claim
``tests/workers/``       the eleven periodic jobs and ``register_jobs``
``tests/api/``           every route in ``api-surface.md`` §10 + the middleware
                         order + the OpenAPI snapshot payzeno-api reads
``tests/integration/``   the whole graph, real engine, real concurrency
======================  =====================================================

``tests.doubles`` and ``tests.factories`` are importable from every layer, which is why
this package has an ``__init__.py`` at all. ``tests/conftest.py`` owns ``pg_engine``.

The gap this structure is designed around: the first six layers cannot express
concurrency. ``tests/services/conftest.py`` hands one session to every caller, and under
that fixture a check-then-act idempotency guard passes. ``tests/integration/`` is the only
place two connections exist, and it arrived in month nine — after PAY-2041, not before.
"""
