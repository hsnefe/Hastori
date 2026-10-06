"""Writer behaviour around database errors, with the database and broker replaced by fakes."""

import uuid
from typing import Any

import asyncpg
import pytest

from hastori_common.settings import Settings
from hastori_ingestion.parsing import Telemetry, message_id
from hastori_ingestion.service import Ingestion, Pending


def pending(n: int, generation: int = 1) -> Pending:
    device = uuid.uuid4()
    ts = 1_700_000_000.0 + n
    item = Telemetry(uuid.uuid4(), device, ts, {"current_a": 1.0}, message_id(device, ts))
    return Pending(item, mid=n, qos=1, generation=generation)


class Harness(Ingestion):
    """Ingestion with `_write` scripted: items in `poison` fail permanently."""

    def __init__(self) -> None:
        super().__init__(Settings())
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


async def test_clean_batch_is_written_once_and_acked() -> None:
    h = Harness()
    await h.flush([pending(i) for i in range(5)])
    assert h.writes == [[0, 1, 2, 3, 4]]
    assert h.acked == [0, 1, 2, 3, 4]


async def test_poison_row_is_dropped_and_the_rest_survives() -> None:
    h = Harness()
    h.poison = {2}
    await h.flush([pending(i) for i in range(5)])
    assert sorted(h.acked) == [0, 1, 2, 3, 4]  # the bad one is acked too: do not redeliver it
    assert sorted(m for w in h.writes for m in w) == [0, 1, 3, 4]


async def test_transient_error_is_retried_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("hastori_ingestion.service.asyncio.sleep", no_sleep)
    h = Harness()
    h.transient_failures = 3
    await h.flush([pending(i) for i in range(3)])
    assert h.writes == [[0, 1, 2]]
    assert h.acked == [0, 1, 2]


async def test_gives_up_without_acking_after_the_shutdown_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("hastori_ingestion.service.asyncio.sleep", no_sleep)
    h = Harness()
    h.transient_failures = 10**6
    h.deadline = 0.0  # already past
    await h.flush([pending(i) for i in range(3)])
    assert h.acked == []  # left un-acked: the broker redelivers after restart


async def test_acks_from_an_older_connection_are_not_sent() -> None:
    h = Harness()
    h.generation = 2
    await h.flush([pending(0, generation=1), pending(1, generation=2)])
    assert h.acked == [1]


async def test_outbox_event_body_carries_the_message_id() -> None:
    from hastori_ingestion.service import _event_body

    p = pending(7)
    assert p.item is not None
    body: Any = __import__("json").loads(_event_body(p.item))
    assert body["message_id"] == str(p.item.message_id)
    assert body["metrics"] == {"current_a": 1.0}
