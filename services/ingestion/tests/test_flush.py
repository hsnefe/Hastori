"""Writer behaviour around database errors, with the database and broker replaced by fakes."""

import uuid
from typing import Any

import asyncpg
import pytest

from hastori_common.settings import Settings
from hastori_ingestion.parsing import Telemetry, message_id
from hastori_ingestion.service import Ingestion, Pending


def test_settings() -> Settings:
    """Never the developer's .env: the tests must give the same result everywhere."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


def pending(n: int, generation: int = 1) -> Pending:
    device = uuid.uuid4()
    ts = 1_700_000_000.0 + n
    item = Telemetry(uuid.uuid4(), device, ts, {"current_a": 1.0}, message_id(device, ts))
    return Pending(item, mid=n, qos=1, generation=generation)


class Harness(Ingestion):
    """Ingestion with `_write` scripted: items in `poison` fail permanently."""

    def __init__(self) -> None:
        super().__init__(test_settings())
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


async def test_unlisted_database_errors_are_retried_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing table (bad migration) or a failover is the environment's fault, not the row's:
    dropping and acking the messages would be silent data loss."""

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("hastori_ingestion.service.asyncio.sleep", no_sleep)

    class Flaky(Harness):
        failures = [
            asyncpg.UndefinedTableError("no such table"),
            asyncpg.InvalidPasswordError("password authentication failed"),
            asyncpg.ReadOnlySQLTransactionError("read-only transaction"),
        ]

        async def _write(self, batch: list[Pending]) -> None:
            if self.failures:
                raise self.failures.pop(0)
            await super()._write(batch)

    h = Flaky()
    await h.flush([pending(i) for i in range(3)])
    assert h.writes == [[0, 1, 2]]
    assert h.acked == [0, 1, 2]


async def test_real_ack_goes_to_the_current_connection_only() -> None:
    sent: list[tuple[int, int]] = []

    class FakePaho:
        def ack(self, mid: int, qos: int) -> None:
            sent.append((mid, qos))

    class FakeClient:
        _client = FakePaho()

    app = Ingestion(test_settings())
    app._client = FakeClient()  # type: ignore[assignment]
    app.generation = 2
    app.ack([pending(1, generation=1), pending(2, generation=2)])
    assert sent == [(2, 1)]


def test_rejection_log_is_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    from hastori_ingestion.parsing import Rejected

    app = Ingestion(test_settings())
    with caplog.at_level("WARNING", logger="ingestion"):
        for _ in range(1000):
            app._log_rejected(Rejected("stale_ts"), "t")
        app._log_rejected(Rejected("bad_value"), "t")
    assert len(caplog.records) == 2  # one per reason, not one per message


def test_healthz_fails_when_a_background_loop_died() -> None:
    from fastapi import Response

    from hastori_ingestion.service import build_api

    class Done:
        def done(self) -> bool:
            return True

        def get_name(self) -> str:
            return "writer"

    app = Ingestion(test_settings())
    healthz = next(r.endpoint for r in build_api(app).routes if r.path == "/healthz")  # type: ignore[attr-defined]
    ok = Response()
    assert healthz(ok) == {"status": "ok"}
    assert ok.status_code is None or ok.status_code == 200
    app.tasks = [Done()]  # type: ignore[list-item]
    failing = Response()
    body = healthz(failing)
    assert failing.status_code == 503
    assert body["dead"] == "writer"
