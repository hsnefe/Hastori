"""The outbox relay against a real PostgreSQL (the SQL, not a fake of it); RabbitMQ is replaced."""

import uuid
from collections.abc import Sequence
from typing import Any

import asyncpg

from hastori_common.settings import Settings
from hastori_ingestion import metrics
from hastori_ingestion.service import OUTBOX_BATCH, Ingestion


class FakePublisher:
    """Confirms every row, or only the first `limit`."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit
        self.published: list[int] = []

    async def publish_rows(self, rows: Sequence[Any]) -> list[int]:
        ids = [r["id"] for r in rows][: self.limit]
        self.published += ids
        return ids


async def fill(pool: asyncpg.Pool, n: int, age: str = "0 seconds") -> None:
    await pool.executemany(
        "INSERT INTO outbox (message_id, routing_key, body, created_at) "
        f"VALUES ($1, 'telemetry.x.y', '{{}}', now() - interval '{age}')",
        [(uuid.uuid4(),) for _ in range(n)],
    )


def relay_for(pool: asyncpg.Pool, publisher: FakePublisher) -> Ingestion:
    app = Ingestion(Settings(_env_file=None))  # type: ignore[call-arg]
    app.pool = pool
    app.publisher = publisher  # type: ignore[assignment]
    return app


async def test_confirmed_rows_are_deleted_and_the_rest_stays(pool: asyncpg.Pool) -> None:
    await fill(pool, 5)
    publisher = FakePublisher(limit=3)
    app = relay_for(pool, publisher)
    assert await app.relay_once() is False
    assert await pool.fetchval("SELECT count(*) FROM outbox") == 2
    assert len(publisher.published) == 3
    assert metrics.OUTBOX_DEPTH._value.get() == 2


async def test_a_full_batch_asks_to_be_called_again(pool: asyncpg.Pool) -> None:
    await fill(pool, OUTBOX_BATCH + 10)
    app = relay_for(pool, FakePublisher())
    assert await app.relay_once() is True
    assert await app.relay_once() is False
    assert await pool.fetchval("SELECT count(*) FROM outbox") == 0
    assert metrics.OUTBOX_DEPTH._value.get() == 0


async def test_events_past_their_age_are_dropped_and_counted(pool: asyncpg.Pool) -> None:
    await fill(pool, 3, age="2 hours")
    await fill(pool, 2)
    publisher = FakePublisher()
    app = relay_for(pool, publisher)
    before = metrics.OUTBOX_EXPIRED._value.get()
    await app.relay_once()
    assert len(publisher.published) == 2  # the old three never reached the broker
    assert metrics.OUTBOX_EXPIRED._value.get() - before == 3


async def test_the_age_scan_runs_once_a_minute_not_at_every_wake_up(pool: asyncpg.Pool) -> None:
    app = relay_for(pool, FakePublisher())
    await app.relay_once()  # the first call does the scan
    await fill(pool, 1, age="2 hours")
    publisher = FakePublisher()
    app.publisher = publisher  # type: ignore[assignment]
    await app.relay_once()
    assert len(publisher.published) == 1  # not scanned again yet: the old row is still published
