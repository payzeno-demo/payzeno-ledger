"""add transaction idempotency key

Every ledger transaction gets a deterministic key derived from the business fact it
records, so posting the same fact twice is a no-op. Format is
``<purpose>:<scope_id>:<subject_id>``, built by ``app/domain/idempotency.py::ledger_key``.

Revision ID: 0007
Revises: 0006
Create Date: month 3 — mregression
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
