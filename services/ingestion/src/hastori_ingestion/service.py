"""Ingestion wiring.

MQTT (shared subscription, manual acks) -> bounded queue -> batch writer
-> one DB transaction (measurements + outbox) -> MQTT ack -> relay -> RabbitMQ.

Delivery guarantees:
- A message is acked to the broker only after its DB transaction committed; after a crash the
  broker redelivers it and ON CONFLICT DO NOTHING absorbs the duplicate.
- The RabbitMQ event is written to the outbox in the same transaction as the measurements, so
  an event can never be lost between "committed" and "published".
"""

import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiomqtt
import asyncpg
import uvicorn
from fastapi import FastAPI, Response
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from hastori_common.dberrors import PERMANENT_DB_ERRORS, RETRYABLE_DB_ERRORS
from hastori_common.logging import configure_logging
from hastori_common.messaging import routing_key
from hastori_common.serve import Server
from hastori_common.settings import Settings, get_settings
from hastori_ingestion import metrics
from hastori_ingestion.batching import BatchPolicy
from hastori_ingestion.parsing import (
    Rejected,
    Telemetry,
    build_telemetry,
    parse_payload,
    parse_topic,
)
from hastori_ingestion.publisher import Publisher

log = logging.getLogger("ingestion")

SHARED_TOPIC = "$share/ingestion/sites/+/devices/+/telemetry"
QUEUE_SIZE = 10_000
CACHE_REFRESH_S = 60.0
# The broker keeps a session's queue this long; parsing.MAX_AGE_S must cover it.
SESSION_EXPIRY_S = 3600
BACKOFF_MAX_S = 30.0
OUTBOX_BATCH = 500
OUTBOX_MAX_AGE = "1 hour"
RELAY_TIMEOUT_S = 60.0
DB_COMMAND_TIMEOUT_S = 30.0
REJECT_LOG_INTERVAL_S = 10.0

INSERT_SQL = (
    "INSERT INTO measurements (time, device_id, metric, value) VALUES ($1, $2, $3, $4) "
    "ON CONFLICT DO NOTHING"
)
OUTBOX_SQL = (
    "INSERT INTO outbox (message_id, routing_key, body) VALUES ($1, $2, $3) "
    "ON CONFLICT (message_id) DO NOTHING"
)

_STOP = object()


@dataclass(frozen=True)
class Pending:
    """A validated message waiting for its DB commit, with what is needed to ack it."""

    item: Telemetry | None
    mid: int
    qos: int
    generation: int


class State:
    """Readiness flags shared with the HTTP endpoints."""

    mqtt = False
    db = False


class Ingestion:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue: asyncio.Queue[Pending | object] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.policy = BatchPolicy()
        self.publisher = Publisher(settings.rabbitmq_url)
        self.pool: asyncpg.Pool | None = None
        # device_id -> (site_id, is_active)
        self.devices: dict[Any, tuple[Any, bool]] = {}
        self.state = State()
        self.last_refresh = 0.0
        self.generation = 0
        self._client: aiomqtt.Client | None = None
        self.stop_event = asyncio.Event()
        self.writer_done = asyncio.Event()
        self.relay_wakeup = asyncio.Event()
        self.devices_changed = asyncio.Event()
        self._drained = False
        self.deadline: float | None = None
        self.tasks: list[asyncio.Task[Any]] = []
        self._reject_logged: dict[str, float] = {}

    # -- device cache -----------------------------------------------------------------------
    async def refresh_devices(self) -> None:
        assert self.pool is not None
        rows = await self.pool.fetch("SELECT id, site_id, is_active FROM devices")
        self.devices = {r["id"]: (r["site_id"], r["is_active"]) for r in rows}
        self.last_refresh = time.monotonic()

    async def cache_loop(self, dsn: str) -> None:
        """Refresh on `devices_changed` notifications, and at least every CACHE_REFRESH_S."""
        listener: asyncpg.Connection | None = None
        try:
            while True:
                if listener is None or listener.is_closed():
                    try:
                        listener = await asyncpg.connect(dsn, timeout=10)
                        await listener.add_listener(
                            "devices_changed", lambda *_: self.devices_changed.set()
                        )
                    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
                        listener = None
                        log.warning("device listener unavailable", extra={"error": str(exc)})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.devices_changed.wait(), CACHE_REFRESH_S)
                self.devices_changed.clear()
                try:
                    await self.refresh_devices()
                except Exception as exc:
                    log.warning("device cache refresh failed", extra={"error": str(exc)})
        finally:
            if listener is not None and not listener.is_closed():
                with contextlib.suppress(Exception):
                    await asyncio.shield(listener.close(timeout=2))

    # -- MQTT -> queue ----------------------------------------------------------------------
    def check_message(self, topic: str, payload: bytes, now: float) -> Telemetry:
        site_id, device_id = parse_topic(topic)
        known = self.devices.get(device_id)
        if known is None:
            raise Rejected("unknown_device")
        if not known[1]:
            raise Rejected("inactive_device")
        if known[0] != site_id:
            raise Rejected("site_mismatch")
        ts, values = parse_payload(payload, now)
        return build_telemetry(site_id, device_id, ts, values)

    def ack(self, pending: list[Pending]) -> None:
        """Ack messages to the broker. Acks from an older connection are skipped: the broker
        redelivers those on the new connection and the unique index absorbs the duplicates."""
        client = self._client
        if client is None:
            return
        for p in pending:
            if p.generation == self.generation and p.qos > 0:
                try:
                    client._client.ack(p.mid, p.qos)
                except Exception as exc:
                    log.warning("mqtt ack failed", extra={"error": str(exc)})

    async def handle(self, message: aiomqtt.Message) -> None:
        topic, payload = str(message.topic), bytes(message.payload or b"")
        receipt = Pending(None, message.mid, message.qos, self.generation)
        try:
            try:
                item = self.check_message(topic, payload, time.time())
            except Rejected as exc:
                # A device seeded after startup: re-read the cache, at most every 5 s.
                if exc.reason != "unknown_device" or time.monotonic() - self.last_refresh < 5:
                    raise
                await self.refresh_devices()
                item = self.check_message(topic, payload, time.time())
        except Rejected as exc:
            metrics.REJECTED.labels(reason=exc.reason).inc()
            self._log_rejected(exc, topic)
            self.ack([receipt])  # nothing to store: do not let the broker redeliver it
            return
        metrics.MESSAGES.inc()
        await self.queue.put(Pending(item, message.mid, message.qos, self.generation))

    def _log_rejected(self, exc: Rejected, topic: str) -> None:
        """One WARNING per reason every REJECT_LOG_INTERVAL_S; the counter has the exact number.
        A device flooding bad messages must not flood the log as well."""
        now = time.monotonic()
        last = self._reject_logged.get(exc.reason)
        if last is not None and now - last < REJECT_LOG_INTERVAL_S:
            return
        self._reject_logged[exc.reason] = now
        log.warning(
            "message rejected (logged at most once per %.0f s per reason)",
            REJECT_LOG_INTERVAL_S,
            extra={"reason": exc.reason, "detail": exc.detail, "topic": topic},
        )

    async def _read(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            await self.handle(message)

    async def subscriber_loop(self) -> None:
        s = self.settings
        props = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]
        props.SessionExpiryInterval = SESSION_EXPIRY_S
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                # clean_start=False: a restarted service resumes its session, so messages the
                # broker queued while it was down are delivered. A stable client id is required.
                async with aiomqtt.Client(
                    hostname=s.mqtt_host,
                    port=s.mqtt_port,
                    username=s.mqtt_ingestion_user,
                    password=s.mqtt_ingestion_password,
                    identifier=s.ingest_client_id,
                    protocol=aiomqtt.ProtocolVersion.V5,
                    clean_start=False,
                    properties=props,
                    tls_params=aiomqtt.TLSParameters(ca_certs=s.mqtt_ca_file),
                    # Bounded even if a device ignores QoS 1 and floods; QoS 1 is limited by the
                    # broker's in-flight window anyway.
                    max_queued_incoming_messages=QUEUE_SIZE,
                ) as client:
                    client._client.manual_ack_set(True)
                    self.generation += 1
                    metrics.MQTT_CONNECTS.inc()
                    self._client = client
                    await client.subscribe(SHARED_TOPIC, qos=1)
                    self.state.mqtt = True
                    backoff = 1.0
                    log.info("subscribed", extra={"topic": SHARED_TOPIC})
                    reader = asyncio.create_task(self._read(client))
                    stopper = asyncio.create_task(self.stop_event.wait())
                    done, _ = await asyncio.wait(
                        {reader, stopper}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if reader in done:
                        stopper.cancel()
                        reader.result()
                    else:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
                        await self._drain()  # connection still open, so acks go out
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("mqtt error", extra={"error": str(exc), "retry_in_s": backoff})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
            finally:
                self.state.mqtt = False
                self._client = None
        await self._drain()

    async def _drain(self) -> None:
        """Stop reading, write what is queued, then return (idempotent)."""
        if self._drained:
            return
        self._drained = True
        self.deadline = time.monotonic() + self.settings.ingest_shutdown_deadline_s
        await self.queue.put(_STOP)
        await self.writer_done.wait()

    # -- queue -> DB (+ outbox) -> ack ------------------------------------------------------
    async def writer_loop(self) -> None:
        batch: list[Pending] = []
        first_at: float | None = None
        stopping = False
        try:
            while True:
                timeout = 0.0 if stopping else self.policy.wait_s(first_at, time.monotonic())
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout)
                except TimeoutError:
                    item = None
                if item is _STOP:
                    stopping = True
                elif isinstance(item, Pending):
                    if not batch:
                        first_at = time.monotonic()
                    batch.append(item)
                if batch and (
                    stopping or self.policy.should_flush(len(batch), first_at, time.monotonic())
                ):
                    await self.flush(batch)
                    batch, first_at = [], None
                if stopping and self.queue.empty() and not batch:
                    break
        finally:
            self.writer_done.set()

    def _expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    async def _write(self, batch: list[Pending]) -> None:
        assert self.pool is not None
        items = [p.item for p in batch if p.item is not None]
        rows = [
            (datetime.fromtimestamp(t.ts, UTC), t.device_id, name, value)
            for t in items
            for name, value in t.metrics.items()
        ]
        events = [
            (t.message_id, routing_key(t.site_id, t.device_id), _event_body(t)) for t in items
        ]
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.executemany(INSERT_SQL, rows)
            await conn.executemany(OUTBOX_SQL, events)

    async def _commit(self, batch: list[Pending]) -> bool:
        """Commit with retries on every database error except PERMANENT_DB_ERRORS. False = gave
        up because we are shutting down past the deadline (the messages stay un-acked and the
        broker redelivers them). Permanent errors propagate."""
        backoff = 0.5
        while True:
            try:
                await self._write(batch)
                self.state.db = True
                return True
            except PERMANENT_DB_ERRORS:
                raise
            except RETRYABLE_DB_ERRORS as exc:
                self.state.db = False
                if self._expired():
                    log.error("shutdown deadline: leaving batch un-acked", extra={"n": len(batch)})
                    return False
                log.warning("db write failed", extra={"error": str(exc), "retry_in_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)

    async def flush(self, batch: list[Pending]) -> None:
        good: list[Pending]
        try:
            if not await self._commit(batch):
                return
            good = batch
        except PERMANENT_DB_ERRORS as exc:
            # The batch contains a row the database refuses for good (e.g. a device deleted
            # behind the cache). Find it: commit one by one, drop only the offenders.
            log.warning("batch refused, retrying row by row", extra={"error": str(exc)})
            good = []
            for p in batch:
                try:
                    if await self._commit([p]):
                        good.append(p)
                except PERMANENT_DB_ERRORS as one:
                    metrics.REJECTED.labels(reason="db_rejected").inc()
                    log.warning(
                        "message dropped by database",
                        extra={
                            "message_id": str(p.item.message_id) if p.item else None,
                            "error": str(one),
                        },
                    )
                    self.ack([p])
        self.ack(good)
        metrics.BATCH_SIZE.observe(len(good))
        committed = time.time()
        for p in good:
            if p.item is not None:
                metrics.LAG.observe(max(0.0, committed - p.item.ts))
        self.relay_wakeup.set()

    # -- outbox -> RabbitMQ -----------------------------------------------------------------
    async def relay_once(self) -> bool:
        """Publish one batch of outbox rows; True if more may be waiting.

        No database transaction (and so no row lock) is held while talking to RabbitMQ: a stalled
        broker (memory/disk alarm) must not pin a connection or block vacuum. If two replicas
        publish the same row, the consumer drops the duplicate by message id.
        """
        assert self.pool is not None
        expired = await self.pool.execute(
            f"DELETE FROM outbox WHERE created_at < now() - interval '{OUTBOX_MAX_AGE}'"  # noqa: S608
        )
        n_expired = int(expired.split()[-1])
        if n_expired:
            metrics.OUTBOX_EXPIRED.inc(n_expired)
            log.error("outbox events expired", extra={"n": n_expired})
        rows = await self.pool.fetch(
            "SELECT id, message_id, routing_key, body FROM outbox ORDER BY id LIMIT $1",
            OUTBOX_BATCH,
        )
        done = (
            await asyncio.wait_for(self.publisher.publish_rows(rows), RELAY_TIMEOUT_S)
            if rows
            else []
        )
        if done:
            await self.pool.execute("DELETE FROM outbox WHERE id = ANY($1::bigint[])", done)
        metrics.OUTBOX_DEPTH.set(await self.pool.fetchval("SELECT count(*) FROM outbox"))
        return len(rows) == OUTBOX_BATCH and len(done) == len(rows)

    async def relay_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.relay_wakeup.wait(), 1.0)
            self.relay_wakeup.clear()
            try:
                while await self.relay_once():
                    pass
            except RETRYABLE_DB_ERRORS as exc:
                log.warning("outbox relay failed", extra={"error": str(exc)})
                await asyncio.sleep(1.0)


def _event_body(t: Telemetry) -> str:
    return json.dumps(
        {
            "message_id": str(t.message_id),
            "site_id": str(t.site_id),
            "device_id": str(t.device_id),
            "ts": t.ts,
            "metrics": t.metrics,
        }
    )


def build_api(app: Ingestion) -> FastAPI:
    api = FastAPI(title="Hastori ingestion")

    @api.get("/healthz")
    def healthz(response: Response) -> dict[str, str]:
        """Liveness: every background loop is still running. A loop that died leaves the process
        looking alive while nothing is processed; failing here lets the orchestrator restart it."""
        dead = [t.get_name() for t in app.tasks if t.done() and not app.stop_event.is_set()]
        if dead:
            response.status_code = 503
            return {"status": "failing", "dead": ",".join(dead)}
        return {"status": "ok"}

    @api.get("/readyz")
    def readyz(response: Response) -> dict[str, bool]:
        status = {
            "mqtt": app.state.mqtt,
            "db": app.state.db,
            "rabbitmq": app.publisher.connected,
        }
        if not all(status.values()):
            response.status_code = 503
        return status

    @api.get("/metrics")
    def prometheus() -> Response:
        return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return api


async def run() -> None:
    settings = get_settings(strict=("database_url", "mqtt_ingestion_password", "rabbitmq_url"))
    configure_logging(settings.log_level)
    app = Ingestion(settings)
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    backoff = 1.0
    while app.pool is None:  # DB may still be starting; compose also waits for migrate
        try:
            app.pool = await asyncpg.create_pool(
                dsn,
                min_size=1,
                max_size=4,
                command_timeout=DB_COMMAND_TIMEOUT_S,
                server_settings={"idle_in_transaction_session_timeout": "60000"},
            )
            await app.refresh_devices()
            app.state.db = True
        except (asyncpg.PostgresError, OSError) as exc:
            log.warning("db not ready", extra={"error": str(exc)})
            if app.pool is not None:
                await app.pool.close()
                app.pool = None
            await asyncio.sleep(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 2)
    log.info("devices loaded", extra={"count": len(app.devices)})

    server = Server(
        uvicorn.Config(
            build_api(app), host="0.0.0.0", port=settings.ingest_http_port, log_level="warning"
        )
    )
    http_task = asyncio.create_task(server.serve(), name="http")
    cache_task = asyncio.create_task(app.cache_loop(dsn), name="cache")
    writer_task = asyncio.create_task(app.writer_loop(), name="writer")
    relay_task = asyncio.create_task(app.relay_loop(), name="relay")
    sub_task = asyncio.create_task(app.subscriber_loop(), name="subscriber")
    app.tasks = [http_task, cache_task, writer_task, relay_task, sub_task]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, app.stop_event.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(app.stop_event.set))

    # Watch the loops: if one dies (a bug, an unexpected exception) the process would otherwise
    # keep looking alive while nothing is processed. Exit non-zero so the restart policy
    # applies; everything not yet acked is redelivered by the broker.
    stopper = asyncio.create_task(app.stop_event.wait())
    done, _ = await asyncio.wait({stopper, *app.tasks}, return_when=asyncio.FIRST_COMPLETED)
    if stopper not in done:
        for t in done:
            failure = None if t.cancelled() else t.exception()
            log.critical(
                "background task %s ended unexpectedly, exiting", t.get_name(), exc_info=failure
            )
        for t in app.tasks:
            t.cancel()
        await asyncio.gather(*app.tasks, return_exceptions=True)
        sys.exit(1)

    log.info("shutting down: draining queue")
    await sub_task  # stops reading, writes the queue out, acks, disconnects
    await writer_task
    with contextlib.suppress(Exception):
        await app.relay_once()  # best effort; the outbox survives a restart anyway
    for task in (cache_task, relay_task):
        task.cancel()
    await asyncio.gather(cache_task, relay_task, return_exceptions=True)
    server.should_exit = True
    await http_task
    await app.publisher.close()
    assert app.pool is not None
    await app.pool.close()
    log.info("stopped")
