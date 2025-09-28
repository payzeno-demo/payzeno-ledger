"""`ProcessedEventRepository` — app/repositories/processed_event.py.

The primary key is `(event_id, consumer)` and not `event_id` alone, because two consumers in
this service subscribe to overlapping topics and both legitimately process the same event.

The only method that matters is `claim`, and it is a single statement:

    INSERT INTO processed_event (event_id, consumer) VALUES (...)
    ON CONFLICT DO NOTHING RETURNING event_id

There is no `has_processed` / `mark_processed` pair, and there must not be. A read followed
by a write around a side effect is exactly PAY-2041 with different nouns, and ADR 0011
forbids it in the money path. If you find yourself adding one, read the postmortem first.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.models.events import ProcessedEvent
from app.repositories.processed_event import ProcessedEventRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 4, 16, 0, 30, tzinfo=UTC)


@pytest.fixture
def repo() -> ProcessedEventRepository:
    return ProcessedEventRepository()


async def test_first_claim_succeeds(session, repo: ProcessedEventRepository) -> None:
    assert await repo.claim(session, event_id="evt_pe_1", consumer="payment_events") is True


async def test_second_claim_by_the_same_consumer_fails(
    session, repo: ProcessedEventRepository
) -> None:
    await repo.claim(session, event_id="evt_pe_2", consumer="payment_events")
    await session.flush()

    assert await repo.claim(session, event_id="evt_pe_2", consumer="payment_events") is False


async def test_two_consumers_may_both_claim_the_same_event(
    session, repo: ProcessedEventRepository
) -> None:
    assert await repo.claim(session, event_id="evt_pe_3", consumer="payment_events") is True
    assert await repo.claim(session, event_id="evt_pe_3", consumer="merchant_events") is True


async def test_the_claim_is_one_statement(session, repo: ProcessedEventRepository) -> None:
    """Asserted structurally, because the whole property is "no window between two reads".

    We compile the statement the repository builds and check it is a single INSERT with the
    conflict clause on it. A test that only asserts the return value passes just as happily
    against a SELECT-then-INSERT implementation.
    """
    compiled = str(repo.claim_statement(event_id="evt_pe_4", consumer="payment_events"))

    assert compiled.upper().startswith("INSERT")
    assert "ON CONFLICT" in compiled.upper()
    assert "RETURNING" in compiled.upper()
    assert compiled.upper().count("SELECT") == 0


async def test_claim_writes_the_row_in_the_callers_transaction(
    sessions, repo: ProcessedEventRepository
) -> None:
    # Dedupe and effect share one transaction. If the handler raises, the claim rolls back
    # with it and the redelivery is processed properly instead of being silently swallowed.
    with pytest.raises(RuntimeError):
        async with sessions.begin() as session:
            await repo.claim(session, event_id="evt_pe_5", consumer="payment_events")
            raise RuntimeError("handler blew up")

    async with sessions.begin() as after:
        assert await repo.claim(after, event_id="evt_pe_5", consumer="payment_events") is True


async def test_claim_records_when_it_happened(session, repo: ProcessedEventRepository) -> None:
    await repo.claim(session, event_id="evt_pe_6", consumer="payment_events", at=NOW)
    await session.flush()

    row = await session.get(ProcessedEvent, ("evt_pe_6", "payment_events"))
    assert row is not None
    assert row.processed_at == NOW


async def test_purge_older_than_removes_only_old_rows(
    session, repo: ProcessedEventRepository
) -> None:
    # Retention: the table is a dedupe ledger, not an audit log. It grows forever otherwise.
    await repo.claim(session, event_id="evt_pe_old", consumer="payment_events", at=NOW)
    await session.flush()
    await session.execute(
        text(
            "UPDATE processed_event SET processed_at = processed_at - interval '90 days' "
            "WHERE event_id = 'evt_pe_old'"
        )
    )
    await repo.claim(session, event_id="evt_pe_new", consumer="payment_events", at=NOW)
    await session.flush()

    deleted = await repo.purge_older_than(session, before=NOW)

    assert deleted == 1
    assert await session.get(ProcessedEvent, ("evt_pe_new", "payment_events")) is not None


async def test_primary_key_is_the_pair(session) -> None:
    rows = await session.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'processed_event'::regclass AND i.indisprimary"
        )
    )
    assert {row[0] for row in rows} == {"event_id", "consumer"}
