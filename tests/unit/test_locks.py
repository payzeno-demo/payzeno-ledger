"""`AdvisoryLockManager._key` — app/db/locks.py.

`pg_advisory_xact_lock(int, int)` takes two int4. `_key` hashes an id down to four bytes and
reads them back SIGNED. An unsigned read lands in 0..4294967295 and roughly half of all
generated ids would raise `integer out of range` from Postgres — which is not a subtle
failure, it is the sweep crashing on every other batch.

This module never touches a session; it only pins the arithmetic.
"""

from __future__ import annotations

import hashlib
import random

import pytest

from app.db.locks import AdvisoryLockManager

INT4_MIN = -(2**31)
INT4_MAX = 2**31 - 1


@pytest.fixture
def locks() -> AdvisoryLockManager:
    return AdvisoryLockManager()


def test_key_fits_int4_for_ten_thousand_random_ids(locks: AdvisoryLockManager) -> None:
    rng = random.Random(20260730)
    ns = AdvisoryLockManager.NAMESPACE

    for _ in range(10_000):
        entity_id = f"sb_{rng.getrandbits(80):020x}"
        key = locks._key(ns, entity_id)
        assert INT4_MIN <= key <= INT4_MAX, (entity_id, key)


def test_unsigned_read_would_overflow_int4_for_some_ids() -> None:
    """The regression this whole module exists for.

    Find an id whose 4-byte digest has the high bit set, and show that the unsigned
    interpretation is outside int4 while the signed one is not.
    """
    overflowing = None
    for i in range(5_000):
        candidate = f"sb_{i:016d}"
        digest = hashlib.blake2b(candidate.encode("utf-8"), digest_size=4).digest()
        if int.from_bytes(digest, "big", signed=False) > INT4_MAX:
            overflowing = (candidate, digest)
            break

    assert overflowing is not None, "expected roughly half of all ids to have the high bit set"
    candidate, digest = overflowing
    assert int.from_bytes(digest, "big", signed=False) > INT4_MAX
    assert INT4_MIN <= int.from_bytes(digest, "big", signed=True) <= INT4_MAX


def test_distinct_ids_rarely_collide(locks: AdvisoryLockManager) -> None:
    # 32 bits over 20k ids: a handful of birthday collisions is expected and harmless (a
    # collision costs throughput, never correctness — two unrelated batches serialise).
    ns = AdvisoryLockManager.NAMESPACE
    keys = {locks._key(ns, f"sb_{i:012d}") for i in range(20_000)}
    assert len(keys) > 19_900


def test_batch_and_item_keys_share_the_derivation(locks: AdvisoryLockManager) -> None:
    """Batch and item locks are the SAME function on different namespaced values.

    That is exactly why PAY-2041 was possible: `acquire_item_lock` existed and looked like a
    considered alternative to the batch lock, when in fact the two are different lock
    DOMAINS and taking one excludes nothing that takes the other.
    """
    ns = AdvisoryLockManager.NAMESPACE
    assert locks._key(ns, "sb_X") != locks._key(ns, "ri_X")


