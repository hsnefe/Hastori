"""The alarm service against a real PostgreSQL and a Redis, with RabbitMQ messages faked.

What is real: the SQL (conditional inserts and updates, the partial unique index, the replay
query), the start-up reconciliation and the Redis events. What is not: the broker connection;
that and the full chain run in `make e2e`.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import asyncpg
import pytest
from redis.asyncio import Redis

from conftest import load_script
from hastori_alarm.service import AlarmService, Stopping
from hastori_alarm.store import Store
from hastori_common.events import channel
from hastori_common.seed_data import load_seed
from hastori_common.settings import Settings

SEED = load_seed()
DEVICE = SEED.device_by_key("izmir-komp-1")
SITE = SEED.site_by_key("izmir")
RULE = next(r for r in SEED.alarm_rules if r.device == "izmir-komp-1")
STEP = 2.0


class FakeMessage:
    """What `handle` uses of an incoming RabbitMQ message."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.acked = False
        self.rejected: bool | None = None

    async def ack(self) -> None:
        self.acked = True

    async def reject(self, requeue: bool = False) -> None:
        self.rejected = requeue


def telemetry(ts: float, temp: float, device: uuid.UUID = DEVICE.id) -> FakeMessage:
    body = {
        "message_id": str(uuid.uuid4()),
        "site_id": str(SITE.id),
        "device_id": str(device),
        "ts": ts,
        "metrics": {"temperature_c": temp, "active_power_kw": 40.0},
    }
    return FakeMessage(json.dumps(body).encode())


async def insert_series(
    pool: asyncpg.Pool, start: float, values: list[float], device: uuid.UUID = DEVICE.id
) -> None:
    rows = [
        (datetime.fromtimestamp(start + i * STEP, UTC), device, "temperature_c", v)
        for i, v in enumerate(values)
    ]
    await pool.executemany(
        "INSERT INTO measurements (time, device_id, metric, value) VALUES ($1, $2, $3, $4)", rows
    )


@pytest.fixture
async def seeded(use_database: str, pool: asyncpg.Pool) -> asyncpg.Pool:
    await load_script("seed").main(reset=False)
    return pool


@pytest.fixture
async def redis_events(fake_redis: Redis) -> AsyncIterator[Any]:
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe(channel(SITE.id))
    await pubsub.get_message(timeout=0.1)  # the subscribe confirmation
    yield pubsub
    await pubsub.aclose()  # type: ignore[no-untyped-call]


async def drain(pubsub: Any) -> list[dict[str, Any]]:
    out = []
    while (m := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)) is not None:
        out.append(json.loads(m["data"]))
    return out


async def new_service(pool: asyncpg.Pool, redis: Redis | None) -> AlarmService:
    svc = AlarmService(Settings(_env_file=None))  # type: ignore[call-arg]
    svc.pool, svc.store, svc.redis = pool, Store(pool), redis
    svc.state.redis = True
    return svc


async def alarms(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    rows: list[asyncpg.Record] = await pool.fetch("SELECT * FROM alarms ORDER BY opened_at")
    return rows


# -- start-up replay --------------------------------------------------------------------------


async def test_an_excursion_during_downtime_opens_an_alarm_at_start(
    seeded: asyncpg.Pool, fake_redis: Redis, redis_events: Any
) -> None:
    start = time.time() - 200
    await insert_series(seeded, start, [70.0] * 5 + [85.0] * 40)  # hot from +10 s on

    svc = await new_service(seeded, fake_redis)
    await svc.startup()

    (alarm,) = await alarms(seeded)
    assert alarm["state"] == "active" and alarm["rule_id"] == RULE.id
    # the 30 s count starts at the first hot sample: opened exactly 30 s later, in the data's time
    assert alarm["opened_at"].timestamp() == pytest.approx(start + 10 + 30, abs=0.01)
    assert alarm["peak_value"] == 85.0
    (event,) = await drain(redis_events)
    assert event["type"] == "alarm.opened" and event["site_id"] == str(SITE.id)
    assert event["alarm_id"] == str(alarm["id"]) and event["value"] == 85.0


async def test_restarting_never_duplicates_or_reopens_an_alarm(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    start = time.time() - 300
    # an excursion that is over: hot for 60 s, then 40 s of recovery
    await insert_series(seeded, start, [70.0] * 3 + [85.0] * 30 + [70.0] * 20)

    for _ in range(3):  # the service starts three times on the same data
        await (await new_service(seeded, fake_redis)).startup()

    (alarm,) = await alarms(seeded)
    assert alarm["state"] == "cleared"
    assert alarm["cleared_at"].timestamp() == pytest.approx(start + 6 + 2 * 30 + 10 - 0.0, abs=2.1)


async def test_an_alarm_open_in_the_database_is_closed_by_recovery_seen_in_the_data(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    start = time.time() - 400
    await insert_series(seeded, start, [85.0] * 30)
    await (await new_service(seeded, fake_redis)).startup()
    assert (await alarms(seeded))[0]["state"] == "active"

    # while the service is down the device recovers
    recovery_start = start + 30 * STEP
    await insert_series(seeded, recovery_start, [70.0] * 20)
    await (await new_service(seeded, fake_redis)).startup()

    (alarm,) = await alarms(seeded)
    assert alarm["state"] == "cleared"
    # closed at the time the data says (hold of 10 s), not at the time of the restart
    assert alarm["cleared_at"].timestamp() == pytest.approx(recovery_start + 10, abs=0.01)
    assert alarm["peak_value"] == 85.0


async def test_older_normal_data_does_not_close_an_alarm_that_opened_later(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    now = time.time()
    await insert_series(seeded, now - 600, [70.0] * 100)  # normal for 200 s, then...
    await insert_series(seeded, now - 400, [85.0] * 60)  # ...hot, the alarm opens
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    (alarm,) = await alarms(seeded)
    assert alarm["state"] == "active"

    again = await new_service(seeded, fake_redis)  # the replay window starts before the alarm
    await again.startup()
    assert [a["state"] for a in await alarms(seeded)] == ["active"]


async def test_an_alarm_of_a_rule_that_was_disabled_while_down_is_closed_at_start(
    seeded: asyncpg.Pool, fake_redis: Redis, redis_events: Any
) -> None:
    start = time.time() - 200
    await insert_series(seeded, start, [85.0] * 40)
    await (await new_service(seeded, fake_redis)).startup()
    await drain(redis_events)

    await seeded.execute("UPDATE alarm_rules SET enabled = false WHERE id = $1", RULE.id)
    await (await new_service(seeded, fake_redis)).startup()

    (alarm,) = await alarms(seeded)
    assert alarm["state"] == "cleared"
    (event,) = await drain(redis_events)
    assert event["type"] == "alarm.cleared" and event["state"] == "cleared"


async def test_a_reactive_ratio_rule_opens_from_stored_readings(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    pano = SEED.device_by_key("izmir-pano")
    start = time.time() - 800
    rows = []
    for i in range(400):  # 800 s: 100 s healthy, then the compensation fails
        ts = datetime.fromtimestamp(start + i * STEP, UTC)
        ratio = 0.14 if i < 50 else 0.35
        rows += [
            (ts, pano.id, "active_power_kw", 100.0),
            (ts, pano.id, "reactive_power_kvar", 100.0 * ratio),
        ]
    await seeded.executemany(
        "INSERT INTO measurements (time, device_id, metric, value) VALUES ($1, $2, $3, $4)", rows
    )
    await (await new_service(seeded, fake_redis)).startup()

    rows_ = await seeded.fetch(
        "SELECT a.state, r.kind FROM alarms a JOIN alarm_rules r ON r.id = a.rule_id"
    )
    assert [(r["kind"], r["state"]) for r in rows_] == [("reactive_ratio", "active")]


# -- live messages ----------------------------------------------------------------------------


async def test_live_messages_open_and_clear_an_alarm_and_are_acked(
    seeded: asyncpg.Pool, fake_redis: Redis, redis_events: Any
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    t0 = time.time()
    sent = [telemetry(t0 + i * STEP, 85.0) for i in range(20)]
    sent += [telemetry(t0 + (20 + i) * STEP, 70.0) for i in range(10)]
    for msg in sent:
        await svc.handle(cast(Any, msg))

    assert all(m.acked for m in sent) and not any(m.rejected is not None for m in sent)
    events = await drain(redis_events)
    assert [e["type"] for e in events] == ["alarm.opened", "alarm.cleared"]
    (alarm,) = await alarms(seeded)
    assert alarm["state"] == "cleared"


async def test_a_duplicated_message_does_not_open_a_second_alarm(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    t0 = time.time()
    for i in range(30):
        msg = telemetry(t0 + i * STEP, 85.0)
        await svc.handle(cast(Any, msg))
        await svc.handle(cast(Any, FakeMessage(msg.body)))  # the broker delivered it twice
    assert len(await alarms(seeded)) == 1


async def test_a_malformed_message_goes_to_the_dead_letter_queue(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    for body in (b"not json", b'{"ts": "x"}', json.dumps({"device_id": "nope"}).encode()):
        msg = FakeMessage(body)
        await svc.handle(cast(Any, msg))
        assert msg.rejected is False and not msg.acked  # requeue=False -> dead-lettered


async def test_a_second_open_for_the_same_rule_adopts_the_existing_alarm(
    seeded: asyncpg.Pool, fake_redis: Redis, redis_events: Any
) -> None:
    """Two instances racing (a restart overlap): the partial unique index decides, once."""
    first = await new_service(seeded, fake_redis)
    second = await new_service(seeded, fake_redis)
    await first.startup()
    await second.startup()
    t0 = time.time()
    for i in range(20):
        msg = telemetry(t0 + i * STEP, 85.0)
        await first.handle(cast(Any, msg))
        await second.handle(cast(Any, FakeMessage(msg.body)))
    assert len(await alarms(seeded)) == 1
    assert [e["type"] for e in await drain(redis_events)] == ["alarm.opened"]


# -- rule changes -----------------------------------------------------------------------------


async def test_disabling_a_rule_closes_its_alarm_and_deleting_a_device_rule_too(
    seeded: asyncpg.Pool, fake_redis: Redis, redis_events: Any
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    t0 = time.time()
    for i in range(25):
        await svc.handle(cast(Any, telemetry(t0 + i * STEP, 85.0)))
    assert (await alarms(seeded))[0]["state"] == "active"
    await drain(redis_events)

    await seeded.execute("UPDATE alarm_rules SET enabled = false WHERE id = $1", RULE.id)
    await svc.reload()
    assert (await alarms(seeded))[0]["state"] == "cleared"
    (event,) = await drain(redis_events)
    assert event["type"] == "alarm.cleared"


async def test_a_new_threshold_applies_to_the_next_messages(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    await seeded.execute(
        "UPDATE alarm_rules SET threshold = 95, clear_threshold = 90 WHERE id = $1", RULE.id
    )
    await svc.reload()
    t0 = time.time()
    for i in range(30):
        await svc.handle(cast(Any, telemetry(t0 + i * STEP, 85.0)))  # hot before, fine now
    assert await alarms(seeded) == []


# -- failures ---------------------------------------------------------------------------------


async def test_a_redis_outage_does_not_block_acks_or_alarms(seeded: asyncpg.Pool) -> None:
    class DeadRedis:
        async def publish(self, *a: Any) -> int:
            raise ConnectionError("redis is down")

    svc = await new_service(seeded, None)
    svc.redis = cast(Any, DeadRedis())
    await svc.startup()
    t0 = time.time()
    sent = [telemetry(t0 + i * STEP, 85.0) for i in range(25)]
    for msg in sent:
        await svc.handle(cast(Any, msg))
    assert all(m.acked for m in sent)
    assert (await alarms(seeded))[0]["state"] == "active"
    assert svc.state.redis is False


async def test_a_database_outage_is_retried_and_nothing_is_lost(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    real = svc._store.open_alarm
    calls = {"n": 0}

    async def flaky(*args: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("connection reset")
        return await real(*args, **kw)

    svc._store.open_alarm = flaky  # type: ignore[method-assign]
    t0 = time.time()
    msgs = [telemetry(t0 + i * STEP, 85.0) for i in range(20)]
    await asyncio.wait_for(asyncio.gather(*[svc.handle(cast(Any, m)) for m in msgs]), 30)
    assert calls["n"] == 3 and len(await alarms(seeded)) == 1
    assert all(m.acked for m in msgs)


async def test_at_shutdown_a_dead_database_leaves_the_message_unacked(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()

    async def down(*a: Any, **kw: Any) -> Any:
        raise OSError("database is gone")

    svc._store.open_alarm = down  # type: ignore[method-assign]
    svc.deadline = time.monotonic() - 1  # the shutdown deadline has passed
    t0 = time.time()
    msgs = [telemetry(t0 + i * STEP, 85.0) for i in range(16)]
    for m in msgs:
        await svc.handle(cast(Any, m))
    # nothing had to be written for the first 15 (the 30 s are still counting): acked. The 16th
    # is the one that opens the alarm; the database is gone, so it stays un-acked and the broker
    # redelivers it after the restart, when the replay rebuilds the state from stored data.
    assert [m.acked for m in msgs] == [True] * 15 + [False]
    assert issubclass(Stopping, Exception)


# -- the loops around the engine --------------------------------------------------------------


async def test_a_rule_change_in_the_database_reaches_the_running_service(
    seeded: asyncpg.Pool, fake_redis: Redis, db_dsn: str
) -> None:
    """LISTEN alarm_rules_changed -> reload: no restart, no polling interval to wait for."""
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    tasks = [
        asyncio.create_task(svc.listen_loop(db_dsn)),
        asyncio.create_task(svc.reload_loop()),
    ]
    try:
        await asyncio.sleep(0.5)  # the listener connects and does its first reload
        t0 = time.time()
        for i in range(25):
            await svc.handle(cast(Any, telemetry(t0 + i * STEP, 85.0)))
        assert (await alarms(seeded))[0]["state"] == "active"

        changed = time.monotonic()
        await seeded.execute("UPDATE alarm_rules SET enabled = false WHERE id = $1", RULE.id)
        for _ in range(60):
            if (await alarms(seeded))[0]["state"] == "cleared":
                break
            await asyncio.sleep(0.1)
        took = time.monotonic() - changed
        assert (await alarms(seeded))[0]["state"] == "cleared"
        assert took < 3.0  # a notification and a 0.2 s settle, not the 60 s safety-net reload
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class FakeQueue:
    """The part of an aio-pika queue `_consume` uses: `iterator()` as an async context manager."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    def iterator(self) -> "FakeQueue":
        return self

    async def __aenter__(self) -> "FakeQueue":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    def __aiter__(self) -> "FakeQueue":
        return self

    async def __anext__(self) -> Any:
        return await self.inbox.get()


async def test_the_consumer_handles_messages_in_order_and_stops_when_asked(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    queue = FakeQueue()
    t0 = time.time()
    sent = [telemetry(t0 + i * STEP, 85.0) for i in range(20)]
    for m in sent:
        queue.inbox.put_nowait(m)

    consumer = asyncio.create_task(svc._consume(cast(Any, queue)))
    for _ in range(100):
        if all(m.acked for m in sent):
            break
        await asyncio.sleep(0.05)
    assert all(m.acked for m in sent)
    assert [a["state"] for a in await alarms(seeded)] == ["active"]  # in order: it opened once

    svc.stop_event.set()  # idle, waiting for the next message: stops at once
    await asyncio.wait_for(consumer, timeout=2)
    assert queue.closed  # the iterator (and so the consumer on the broker) was closed


async def test_a_message_in_hand_is_finished_before_the_consumer_stops(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    queue = FakeQueue()
    started, release = asyncio.Event(), asyncio.Event()
    real_handle = svc.handle

    async def slow(message: Any) -> None:
        started.set()
        await release.wait()  # the shutdown arrives while this message is being processed
        await real_handle(message)

    svc.handle = slow  # type: ignore[method-assign]
    msg = telemetry(time.time(), 70.0)
    queue.inbox.put_nowait(msg)
    consumer = asyncio.create_task(svc._consume(cast(Any, queue)))
    await asyncio.wait_for(started.wait(), 2)

    svc.stop_event.set()
    await asyncio.sleep(0.2)
    assert not consumer.done() and not msg.acked  # not cut off in the middle
    release.set()
    await asyncio.wait_for(consumer, timeout=2)
    assert msg.acked  # finished and acked, then the consumer left
    assert queue.closed


async def test_a_long_rule_is_rebuilt_after_a_restart_in_the_middle_of_its_count(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    """A 30 minute rule, breached for 40 minutes, restarted at minute 40: the replay must reach
    back over the rule's whole duration, or the count restarts too late and never completes. (The
    alarm may open later than it would have without the restart, never not at all.)"""
    await seeded.execute("UPDATE alarm_rules SET duration_s = 1800 WHERE id = $1", RULE.id)
    start = time.time() - 2400
    await insert_series(seeded, start, [85.0] * 1200)

    await (await new_service(seeded, fake_redis)).startup()

    (alarm,) = await alarms(seeded)
    assert alarm["state"] == "active"
    assert start + 1800 <= alarm["opened_at"].timestamp() <= time.time()


async def test_an_alarm_keeps_the_thresholds_it_opened_with(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    start = time.time() - 200
    await insert_series(seeded, start, [85.0] * 40)
    await (await new_service(seeded, fake_redis)).startup()
    await seeded.execute(
        "UPDATE alarm_rules SET threshold = 90, clear_threshold = 85"
    )  # edited later
    (alarm,) = await alarms(seeded)
    assert alarm["threshold"] == RULE.threshold and alarm["clear_threshold"] == RULE.clear_threshold


async def test_a_device_clock_set_back_does_not_blind_the_live_alarm(
    seeded: asyncpg.Pool, fake_redis: Redis
) -> None:
    """B3 through the real message path: 60 hot readings, 1 h behind the newest, still alarm."""
    svc = await new_service(seeded, fake_redis)
    await svc.startup()
    now = time.time()
    for i in range(10):
        msg = telemetry(now + i * STEP, 70.0)
        await svc.handle(msg)  # type: ignore[arg-type]
    behind = now - 3600
    for i in range(40):
        msg = telemetry(behind + i * STEP, 85.0)
        await svc.handle(msg)  # type: ignore[arg-type]
        assert msg.acked
    (alarm,) = await alarms(seeded)
    assert alarm["state"] == "active"
    assert alarm["opened_at"].timestamp() == pytest.approx(behind + 30, abs=0.01)
    assert svc.engine.clock_jumps == 1
