"""Idempotency keys and request fingerprints — app/domain/idempotency.py.

`ledger_key` is the only thing that builds `ledger_transaction.idempotency_key`. Since
migration 0020 that column carries a UNIQUE index, so a key that collides across two
logically distinct postings is now a hard failure instead of a silent duplicate — which is
the direction we want, but it means the format is a contract and not a convention.

`fingerprint_of` is what makes `POST /internal/v1/transactions`'s documented behaviour
implementable: same key + same body -> 200 and the existing row; same key + different body
-> 409 duplicate_settlement. Without a stored fingerprint there is nothing to compare.
"""

from __future__ import annotations

import re

import pytest

from app.domain.idempotency import fingerprint_of, ledger_key

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def test_ledger_key_format(purpose: str, scope: str, subject: str, expected: str) -> None:
    assert ledger_key(purpose, scope, subject) == expected


def test_ledger_key_rejects_an_empty_component() -> None:
    # An empty subject collapses `settle:sb_A:` into a prefix that the 0007 backfill also
    # produced. That backfill is exactly why the index could not be unique for six months.
    with pytest.raises(ValueError, match="component"):
        ledger_key("settle", "sb_A", "")
    with pytest.raises(ValueError, match="component"):
        ledger_key("", "sb_A", "ri_B")


def test_ledger_key_rejects_a_component_containing_the_separator() -> None:
    with pytest.raises(ValueError, match="separator"):
        ledger_key("settle", "sb_A:B", "ri_C")


def test_fingerprint_is_a_sha256_hex_digest() -> None:
    assert SHA256_HEX.match(fingerprint_of(_FakeItem()))


def test_fingerprint_is_stable_across_calls() -> None:
    assert fingerprint_of(_FakeItem()) == fingerprint_of(_FakeItem())


def test_fingerprint_ignores_mutable_attempt_bookkeeping() -> None:
    # attempt_count and last_attempt_at change on every retry. If they fed the fingerprint,
    # the second attempt at the same item would look like a different request body and
    # POST /internal/v1/transactions would answer 409 instead of 200.
    baseline = fingerprint_of(_FakeItem())
    assert fingerprint_of(_FakeItem(attempt_count=4, last_error_code="rate_limited")) == baseline
