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


@pytest.mark.parametrize(
    ("purpose", "scope", "subject", "expected"),
    [
        ("settle", "sb_01HQ8ZK4QK", "ri_01HQ8ZM7X2", "settle:sb_01HQ8ZK4QK:ri_01HQ8ZM7X2"),
        ("capture", "sb_01HQ8ZK4QK", "ch_01HQ8ZP1AA", "capture:sb_01HQ8ZK4QK:ch_01HQ8ZP1AA"),
        ("payout", "mer_01HQ8ZR9BB", "po_01HQ8ZS3CC", "payout:mer_01HQ8ZR9BB:po_01HQ8ZS3CC"),
    ],
)
def test_ledger_key_format(purpose: str, scope: str, subject: str, expected: str) -> None:
    assert ledger_key(purpose, scope, subject) == expected


def test_ledger_key_is_deterministic() -> None:
    assert ledger_key("settle", "sb_A", "ri_B") == ledger_key("settle", "sb_A", "ri_B")


def test_ledger_key_separates_purposes_over_the_same_subject() -> None:
    # A settled sale posts `settle` and, for capture_at_settlement merchants, a `capture`
    # against the acquirer keyed on the same batch. They must not share a key.
    assert ledger_key("settle", "sb_A", "ri_B") != ledger_key("capture", "sb_A", "ri_B")


def test_ledger_key_subject_for_settlement_is_the_item_not_the_charge() -> None:
    """domain-model.md §0.3 — a charge legitimately appears in two batches.

    An original sale and a later chargeback representment both reference `ch_C`. Keying on
    the charge would make the second batch's line a duplicate of the first and it would
    silently never post.
    """
    first_batch = ledger_key("settle", "sb_ORIGINAL", "ri_1")
    representment = ledger_key("settle", "sb_REPRESENTMENT", "ri_2")
    assert first_batch != representment


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


class _FakeItem:
    """Minimal stand-in with the attribute surface `fingerprint_of` reads."""

    def __init__(self, **kw: object) -> None:
        self.id = "ri_01HQ8ZM7X2"
        self.batch_id = "sb_01HQ8ZK4QK"
        self.charge_id = "ch_01HQ8ZP1AA"
        self.merchant_id = "mer_01HQ8ZR9BB"
        self.currency = "USD"
        self.livemode = True
        self.line_type = "sale"
        self.gross_minor = 10_000
        self.fee_minor = 290
        self.net_minor = 9_710
        self.interchange_minor = 150
        self.scheme_fee_minor = 13
        self.__dict__.update(kw)


def test_fingerprint_is_a_sha256_hex_digest() -> None:
    assert SHA256_HEX.match(fingerprint_of(_FakeItem()))


def test_fingerprint_is_stable_across_calls() -> None:
    assert fingerprint_of(_FakeItem()) == fingerprint_of(_FakeItem())


def test_fingerprint_changes_when_the_money_changes() -> None:
    baseline = fingerprint_of(_FakeItem())
    assert fingerprint_of(_FakeItem(gross_minor=10_001)) != baseline
    assert fingerprint_of(_FakeItem(fee_minor=291)) != baseline
    assert fingerprint_of(_FakeItem(currency="EUR")) != baseline
    assert fingerprint_of(_FakeItem(livemode=False)) != baseline


def test_fingerprint_ignores_mutable_attempt_bookkeeping() -> None:
    # attempt_count and last_attempt_at change on every retry. If they fed the fingerprint,
    # the second attempt at the same item would look like a different request body and
    # POST /internal/v1/transactions would answer 409 instead of 200.
    baseline = fingerprint_of(_FakeItem())
    assert fingerprint_of(_FakeItem(attempt_count=4, last_error_code="rate_limited")) == baseline
