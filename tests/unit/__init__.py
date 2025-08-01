"""Unit layer — pure logic only.

Everything under this package exercises `app/domain/**`, `app/errors.py`, `app/flags.py`
and `app/db/locks.py` with no database, no event loop I/O and no network. CI enforces
100% line coverage on `app/domain/**` off the back of these files (see pyproject's
``[tool.coverage]`` gates and .github/workflows/ci.yml).

This package needs `__init__.py`: `tests/repositories/test_base.py` and
`tests/consumers/test_base.py` share a basename and pytest's prepend import mode would
collide on the bare module name.
"""
