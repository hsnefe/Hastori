"""Live (Redis) events: published after the commit, never in the way of a write or an ack."""

import asyncio
import json
import uuid
from typing import Any

import asyncpg
import pytest

from hastori_common.events import MeasurementEvent, channel
from hastori_common.settings import Settings
from hastori_ingestion.parsing import Telemetry, message_id
from hastori_ingestion.service import EVENT_QUEUE_SIZE, Ingestion, Pending


class Harness(Ingestion):
    """Ingestion with the database replaced: `_write` records, items in `poison` are refused."""

    def __init__(self) -> None:
        super().__init__(Settings(_env_file=None))  # type: ignore[call-arg]
        self.poison: set[int] = set()
        self.transient_failures = 0
        self.acked: list[int] = []
        self.writes: list[list[int]] = []
        self.generation = 1

    async def _write(self, batch: list[Pending]) -> None:
        if self.transient_failures:
            self.transient_failures -= 1
            raise OSError("connection reset")
        if any(p.mid in self.poison for p in batch):
            raise asyncpg.ForeignKeyViolationError("device is gone")
        self.writes.append([p.mid for p in batch])

    def ack(self, pending: list[Pending]) -> None:
        self.acked += [p.mid for p in pending if p.generation == self.generation]


def pending(n: int, site: uuid.UUID, device: uuid.UUID | None = None) -> Pending:
    device = device or uuid.uuid4()
    ts = 1_700_000_000.0 + n
    item = Telemetry(site, device, ts, {"active_power_kw": 40.0 + n}, message_id(device, ts))
    return Pending(item, mid=n, qos=1, generation=1)


class Recorder:
    """A Redis client that records PUBLISH calls (or fails them)."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.published: list[tuple[str, str]] = []
        self.fail = fail

    def pipeline(self, transaction: bool = True) -> "Recorder":
        return self

    async def __aenter__(self) -> "Recorder":
        self._batch: list[tuple[str, str]] = []
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def publish(self, chan: str, body: str) -> None:
        self._batch.append((chan, body))

    async def execute(self) -> None:
        if self.fail is not None:
            raise self.fail
        self.published += self._batch


async def test_a_committed_batch_is_published_to_the_channel_of_its_site() -> None:
    h = Harness()
    h.redis = Recorder()  # type: ignore[assignment]
    izmir, antalya = uuid.uuid4(), uuid.uuid4()
    batch = [pending(0, izmir), pending(1, antalya), pending(2, izmir)]
    await h.flush(batch)
    await h.publish_events(h.event_queue.get_nowait())
    rec: Recorder = h.redis  # type: ignore[assignment]
    assert [c for c, _ in rec.published] == [channel(izmir), channel(antalya), channel(izmir)]
    first = batch[0].item
    assert first is not None
    body = json.loads(rec.published[0][1])
    assert body == {
        "type": "measurement",
        "device_id": str(first.device_id),
        "ts": first.ts,
        "metrics": {"active_power_kw": 40.0},
    }
    assert MeasurementEvent.model_validate_json(rec.published[0][1]).ts == first.ts  # same schema


async def test_a_batch_that_did_not_commit_is_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("hastori_ingestion.service.asyncio.sleep", no_sleep)
    h = Harness()
    h.redis = Recorder()  # type: ignore[assignment]
    h.transient_failures = 10**6
    h.deadline = 0.0  # shutting down: gives up without committing
    await h.flush([pending(0, uuid.uuid4())])
    assert h.acked == [] and h.event_queue.empty()


async def test_a_row_the_database_refused_is_not_published() -> None:
    h = Harness()
    h.redis = Recorder()  # type: ignore[assignment]
    h.poison = {1}
    await h.flush([pending(i, uuid.uuid4()) for i in range(3)])
    events = h.event_queue.get_nowait()
    assert len(events) == 2 and sorted(h.acked) == [0, 1, 2]


async def test_redis_failures_are_counted_and_do_not_touch_the_write_or_the_ack() -> None:
    from hastori_ingestion import metrics

    h = Harness()
    h.redis = Recorder(fail=ConnectionError("redis is down"))  # type: ignore[assignment]
    before = metrics.EVENT_PUBLISH_FAILURES._value.get()
    await h.flush([pending(i, uuid.uuid4()) for i in range(4)])
    assert h.acked == [0, 1, 2, 3] and h.writes == [[0, 1, 2, 3]]  # written and acked as usual
    await h.publish_events(h.event_queue.get_nowait())  # the publisher task meets the outage
    assert metrics.EVENT_PUBLISH_FAILURES._value.get() == before + 4


async def test_a_hung_redis_is_given_up_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hastori_ingestion.service.EVENT_PUBLISH_TIMEOUT_S", 0.05)

    class Hung(Recorder):
        async def execute(self) -> None:
            await asyncio.sleep(60)

    h = Harness()
    h.redis = Hung()  # type: ignore[assignment]
    await h.flush([pending(0, uuid.uuid4())])
    await asyncio.wait_for(h.publish_events(h.event_queue.get_nowait()), 2)


async def test_a_full_event_queue_drops_events_without_blocking() -> None:
    from hastori_ingestion import metrics

    h = Harness()
    h.redis = Recorder()  # type: ignore[assignment]
    for _ in range(EVENT_QUEUE_SIZE):
        h.event_queue.put_nowait([("c", "{}")])
    before = metrics.EVENT_PUBLISH_FAILURES._value.get()
    await asyncio.wait_for(h.flush([pending(0, uuid.uuid4()), pending(1, uuid.uuid4())]), 1)
    assert h.acked == [0, 1]
    assert metrics.EVENT_PUBLISH_FAILURES._value.get() == before + 2


async def test_without_a_redis_client_nothing_is_queued() -> None:
    h = Ingestion(Settings(_env_file=None))  # type: ignore[call-arg]
    h.queue_events([pending(0, uuid.uuid4())])
    assert h.event_queue.empty()


async def test_the_events_task_is_part_of_the_liveness_check() -> None:
    import inspect

    from hastori_ingestion import service

    assert '"events"' in inspect.getsource(service.run)
