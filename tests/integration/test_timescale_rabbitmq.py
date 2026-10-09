"""Real TimescaleDB and RabbitMQ in containers (Testcontainers): the paths the fast tests fake
(risk F5). The same images as compose; migration 0002 (hypertable, continuous aggregate,
retention) really runs.

Needs Docker and is not part of the default `pytest` run: `make test-integration` (CI runs it as
a job of its own). One database is migrated and seeded per session; each test starts from empty
data tables and queues.
"""

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass

import aio_pika
import asyncpg
import pytest
from alembic import command

from conftest import alembic_config, load_script
from hastori_alarm.service import AlarmService
from hastori_alarm.store import Store
from hastori_common.messaging import ALARM_QUEUE, DLQ, EXCHANGE, declare_topology, routing_key
from hastori_common.seed_data import load_seed
from hastori_common.settings import Settings
from hastori_ingestion.parsing import build_telemetry
from hastori_ingestion.publisher import Publisher
from hastori_ingestion.service import _STOP, Ingestion, Pending

pytestmark = pytest.mark.integration

# Pinned in docker-compose.yml: test what runs.
TIMESCALE_IMAGE = "timescale/timescaledb:2.17.2-pg17"
RABBITMQ_IMAGE = "rabbitmq:4.3.6-management"
# First start of a TimescaleDB container runs initdb and a temporary server, then the real one:
# its "ready" line comes twice (risk F4). Waiting for a real query over TCP sidesteps that (the
# temporary server listens on the unix socket only). Cold CI runners pull the image in this time.
READY_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Stack:
    dsn: str  # plain postgresql://
    amqp: str


def _docker_or_skip() -> None:
    try:
        import docker

        docker.from_env().ping()
    except Exception as exc:  # no daemon, no socket, no permission
        pytest.skip(f"Docker is not available: {exc}")


async def _wait_postgres(dsn: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
            try:
                await conn.fetchval("SELECT 1")
                return
            finally:
                await conn.close()
        except (TimeoutError, OSError, asyncpg.PostgresError):
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(1)


async def _wait_rabbitmq(url: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            conn = await aio_pika.connect(url, timeout=5)
            await conn.close()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(1)


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    _docker_or_skip()
    from testcontainers.core.container import DockerContainer

    pg = (
        DockerContainer(TIMESCALE_IMAGE)
        .with_env("POSTGRES_USER", "hastori")
        .with_env("POSTGRES_PASSWORD", "integration")
        .with_env("POSTGRES_DB", "hastori")
        .with_exposed_ports(5432)
    )
    mq = (
        DockerContainer(RABBITMQ_IMAGE)
        .with_env("RABBITMQ_DEFAULT_USER", "hastori")
        .with_env("RABBITMQ_DEFAULT_PASS", "integration")
        .with_exposed_ports(5672)
    )
    pg.start()
    try:
        mq.start()
        try:
            host = pg.get_container_host_ip()
            dsn = f"postgresql://hastori:integration@{host}:{pg.get_exposed_port(5432)}/hastori"
            amqp = (
                f"amqp://hastori:integration@{mq.get_container_host_ip()}:"
                f"{mq.get_exposed_port(5672)}/"
            )
            asyncio.run(_wait_postgres(dsn))
            asyncio.run(_wait_rabbitmq(amqp))
            _migrate_and_seed(dsn)
            yield Stack(dsn, amqp)
        finally:
            mq.stop()
    finally:
        pg.stop()


def _migrate_and_seed(dsn: str) -> None:
    from hastori_common.settings import get_settings

    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "SEED_SYSTEM_ADMIN_PASSWORD")}
    try:
        command.upgrade(alembic_config(dsn), "head")  # sets DATABASE_URL for the seed too
        os.environ["SEED_SYSTEM_ADMIN_PASSWORD"] = "integration-admin-password"
        get_settings.cache_clear()
        asyncio.run(load_script("seed").main(reset=False))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.fixture
async def pool(stack: Stack) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(stack.dsn, min_size=1, max_size=4)
    assert p is not None
    await p.execute("TRUNCATE measurements, outbox, alarms")
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def amqp(stack: Stack) -> AsyncIterator[aio_pika.abc.AbstractChannel]:
    """A channel on the real broker, with the topology declared and both queues empty."""
    conn = await aio_pika.connect(stack.amqp)
    channel = await conn.channel()
    await declare_topology(channel)
    for name in (ALARM_QUEUE, DLQ):
        await (await channel.get_queue(name)).purge()
    try:
        yield channel
    finally:
        await conn.close()


# -- tests ----------------------------------------------------------------------------------

SEED = load_seed()
SITE = SEED.site_by_key("izmir")
DEVICE = SEED.device_by_key("izmir-komp-1")
RULE = next(r for r in SEED.alarm_rules if r.device == "izmir-komp-1" and r.kind == "threshold")


def settings(stack: Stack) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=stack.dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
        rabbitmq_url=stack.amqp,
    )


async def depth(channel: aio_pika.abc.AbstractChannel, name: str) -> int:
    queue = await channel.declare_queue(name, passive=True)
    count = queue.declaration_result.message_count
    assert count is not None
    return count


async def eventually(check: Callable[[], Awaitable[bool]], within_s: float = 20.0) -> None:
    """Poll an async predicate until it holds."""
    deadline = time.monotonic() + within_s
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.2)


# -- migration 0002 -------------------------------------------------------------------------


async def test_migrations_build_the_hypertable_aggregate_and_policies(pool: asyncpg.Pool) -> None:
    hypertables = await pool.fetch(
        "SELECT hypertable_name FROM timescaledb_information.hypertables"
    )
    assert {r["hypertable_name"] for r in hypertables} >= {"measurements"}
    (cagg,) = await pool.fetch(
        "SELECT view_name, materialized_only FROM timescaledb_information.continuous_aggregates"
    )
    assert cagg["view_name"] == "measurements_1m" and cagg["materialized_only"] is False
    jobs = await pool.fetch(
        "SELECT proc_name, hypertable_name, config FROM timescaledb_information.jobs "
        "WHERE proc_name IN ('policy_retention', 'policy_refresh_continuous_aggregate')"
    )
    retention = {
        r["hypertable_name"]: json.loads(r["config"])["drop_after"]
        for r in jobs
        if r["proc_name"] == "policy_retention"
    }
    assert retention.get("measurements") == "7 days"
    assert any(r["proc_name"] == "policy_refresh_continuous_aggregate" for r in jobs)
    head = await pool.fetchval("SELECT version_num FROM alembic_version")
    assert head is not None


# -- ingestion: writer and outbox relay -----------------------------------------------------


def pending(ts: float, temp: float, mid: int) -> Pending:
    item = build_telemetry(SITE.id, DEVICE.id, ts, {"temperature_c": temp, "current_a": 10.0})
    return Pending(item, mid, 1, 0)


async def test_the_writer_stores_each_reading_once_with_its_outbox_event(
    stack: Stack, pool: asyncpg.Pool
) -> None:
    app = Ingestion(settings(stack))
    app.pool = pool
    now = round(time.time(), 3)
    batch = [pending(now - 10 + i * 2, 70.0 + i, i) for i in range(5)]
    for p in [*batch, batch[0], batch[3]]:  # two redeliveries after a lost ack
        await app.queue.put(p)
    await app.queue.put(_STOP)
    await asyncio.wait_for(app.writer_loop(), 30)

    assert await pool.fetchval("SELECT count(*) FROM measurements") == 10  # 5 readings x 2
    assert await pool.fetchval("SELECT count(*) FROM outbox") == 5
    # The continuous aggregate answers for the not yet materialised minutes (real-time mode).
    peak = await pool.fetchval(
        "SELECT max(max_value) FROM measurements_1m WHERE device_id = $1 AND metric = $2",
        DEVICE.id,
        "temperature_c",
    )
    assert peak == 74.0


async def test_the_relay_publishes_the_outbox_to_the_alarm_queue_in_order(
    stack: Stack, pool: asyncpg.Pool, amqp: aio_pika.abc.AbstractChannel
) -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    key = routing_key(SITE.id, DEVICE.id)
    await pool.executemany(
        "INSERT INTO outbox (message_id, routing_key, body) VALUES ($1, $2, $3)",
        [(mid, key, json.dumps({"n": n})) for n, mid in enumerate(ids)],
    )
    app = Ingestion(settings(stack))
    app.pool = pool
    try:
        assert await app.relay_once() is False
    finally:
        await app.publisher.close()

    assert await pool.fetchval("SELECT count(*) FROM outbox") == 0
    queue = await amqp.get_queue(ALARM_QUEUE)
    got = []
    for _ in ids:
        message = await queue.get(timeout=5)
        await message.ack()
        got.append((message.message_id, json.loads(message.body)["n"]))
    assert got == [(str(mid), n) for n, mid in enumerate(ids)]


async def test_the_publisher_keeps_rows_while_the_broker_is_unreachable(
    stack: Stack, pool: asyncpg.Pool
) -> None:
    await pool.execute(
        "INSERT INTO outbox (message_id, routing_key, body) VALUES ($1, 'telemetry.x.y', '{}')",
        uuid.uuid4(),
    )
    app = Ingestion(settings(stack))
    app.pool = pool
    app.publisher = Publisher("amqp://hastori:integration@127.0.0.1:1/")  # nothing listens
    assert await app.relay_once() is False
    assert await pool.fetchval("SELECT count(*) FROM outbox") == 1


# -- alarm service: the real consumer ---------------------------------------------------------


async def publish(channel: aio_pika.abc.AbstractChannel, body: bytes) -> None:
    exchange = await channel.get_exchange(EXCHANGE)
    await exchange.publish(
        aio_pika.Message(body, content_type="application/json"),
        routing_key=routing_key(SITE.id, DEVICE.id),
    )


def reading(ts: float, temp: float) -> bytes:
    return json.dumps(
        {
            "message_id": str(uuid.uuid4()),
            "site_id": str(SITE.id),
            "device_id": str(DEVICE.id),
            "ts": ts,
            "metrics": {"temperature_c": temp},
        }
    ).encode()


async def test_the_alarm_consumer_opens_an_alarm_and_dead_letters_garbage(
    stack: Stack, pool: asyncpg.Pool, amqp: aio_pika.abc.AbstractChannel
) -> None:
    svc = AlarmService(settings(stack))
    svc.pool, svc.store = pool, Store(pool)
    await svc.startup()
    consumer = asyncio.create_task(svc.consumer_loop())
    try:

        async def consuming() -> bool:
            return svc.rabbitmq_ready

        await eventually(consuming)
        start = time.time() - 60
        for i in range(25):  # 48 s above 80 C, one reading every 2 s
            await publish(amqp, reading(start + i * 2, 85.0))
        await publish(amqp, b"not json")

        async def alarm_open() -> bool:
            n = await pool.fetchval(
                "SELECT count(*) FROM alarms WHERE rule_id = $1 AND state = 'active'", RULE.id
            )
            return bool(n == 1)

        await eventually(alarm_open)
        opened = await pool.fetchval("SELECT opened_at FROM alarms WHERE rule_id = $1", RULE.id)
        # 30 s after the first hot reading, in the readings' own time
        assert abs(opened.timestamp() - (start + 30)) < 0.01

        async def settled() -> bool:
            return await depth(amqp, ALARM_QUEUE) == 0 and await depth(amqp, DLQ) == 1

        await eventually(settled)  # every good message acked, the bad one dead-lettered
    finally:
        svc.stop_event.set()
        await asyncio.wait_for(consumer, 15)
