"""Ingestion wiring: MQTT subscriber -> bounded queue -> batch writer -> RabbitMQ."""

import asyncio
import logging
import signal
import socket
import time
import uuid
from datetime import UTC, datetime

import aiomqtt
import asyncpg
import uvicorn
from fastapi import FastAPI, Response
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from hastori_common.logging import configure_logging
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
SESSION_EXPIRY_S = 3600
BACKOFF_MAX_S = 30.0

INSERT_SQL = (
    "INSERT INTO measurements (time, device_id, metric, value) VALUES ($1, $2, $3, $4) "
    "ON CONFLICT DO NOTHING"
)

_STOP = object()


class State:
    """Readiness flags shared with the HTTP endpoints."""

    mqtt = False
    db = False


class Ingestion:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue: asyncio.Queue[Telemetry | object] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.policy = BatchPolicy()
        self.publisher = Publisher(settings.rabbitmq_url)
        self.pool: asyncpg.Pool | None = None
        # device_id -> (site_id, active)
        self.devices: dict[uuid.UUID, tuple[uuid.UUID, bool]] = {}
        self.state = State()
        self.last_refresh = 0.0

    # -- device cache -----------------------------------------------------------------------
    async def refresh_devices(self) -> None:
        assert self.pool is not None
        rows = await self.pool.fetch("SELECT id, site_id, active FROM devices")
        self.devices = {r["id"]: (r["site_id"], r["active"]) for r in rows}
        self.last_refresh = time.monotonic()

    async def cache_loop(self) -> None:
        while True:
            await asyncio.sleep(CACHE_REFRESH_S)
            try:
                await self.refresh_devices()
            except Exception as exc:
                log.warning("device cache refresh failed", extra={"error": str(exc)})

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

    async def subscriber_loop(self) -> None:
        s = self.settings
        props = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]
        props.SessionExpiryInterval = SESSION_EXPIRY_S
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=s.mqtt_host,
                    port=s.mqtt_port,
                    username=s.mqtt_ingestion_user,
                    password=s.mqtt_ingestion_password,
                    identifier=f"ingestion-{socket.gethostname()}",
                    protocol=aiomqtt.ProtocolVersion.V5,
                    properties=props,
                    tls_params=aiomqtt.TLSParameters(ca_certs=s.mqtt_ca_file),
                ) as client:
                    await client.subscribe(SHARED_TOPIC, qos=1)
                    self.state.mqtt = True
                    backoff = 1.0
                    log.info("subscribed", extra={"topic": SHARED_TOPIC})
                    async for message in client.messages:
                        await self.handle(str(message.topic), bytes(message.payload or b""))
            except aiomqtt.MqttError as exc:
                self.state.mqtt = False
                log.warning("mqtt error", extra={"error": str(exc), "retry_in_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
            finally:
                self.state.mqtt = False

    async def handle(self, topic: str, payload: bytes) -> None:
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
            log.warning(
                "message rejected",
                extra={"reason": exc.reason, "detail": exc.detail, "topic": topic},
            )
            return
        metrics.MESSAGES.inc()
        await self.queue.put(item)  # blocks when full: reading stops, broker keeps the rest

    # -- queue -> DB -> RabbitMQ ------------------------------------------------------------
    async def writer_loop(self) -> None:
        batch: list[Telemetry] = []
        first_at: float | None = None
        stopping = False
        while not stopping or batch:
            timeout = self.policy.wait_s(first_at, time.monotonic())
            if stopping:
                timeout = 0
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout)
            except TimeoutError:
                item = None
            if item is _STOP:
                stopping = True
            elif isinstance(item, Telemetry):
                if not batch:
                    first_at = time.monotonic()
                batch.append(item)
            now = time.monotonic()
            if batch and (stopping or self.policy.should_flush(len(batch), first_at, now)):
                await self.flush(batch)
                batch, first_at = [], None
            if stopping and self.queue.empty() and not batch:
                break

    async def flush(self, batch: list[Telemetry]) -> None:
        rows = [
            (datetime.fromtimestamp(t.ts, UTC), t.device_id, name, value)
            for t in batch
            for name, value in t.metrics.items()
        ]
        assert self.pool is not None
        backoff = 0.5
        while True:
            try:
                await self.pool.executemany(INSERT_SQL, rows)
                self.state.db = True
                break
            except (asyncpg.PostgresError, OSError) as exc:
                self.state.db = False
                log.warning("db write failed", extra={"error": str(exc), "retry_in_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
        metrics.BATCH_SIZE.observe(len(batch))
        committed = time.time()
        for t in batch:
            metrics.LAG.observe(max(0.0, committed - t.ts))
        await self.publisher.publish(batch)  # DB first; failures are counted, not raised


def build_api(app: Ingestion) -> FastAPI:
    api = FastAPI(title="Hastori ingestion")

    @api.get("/healthz")
    def healthz() -> dict[str, str]:
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
    settings = get_settings()
    configure_logging(settings.log_level)
    app = Ingestion(settings)
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    backoff = 1.0
    while app.pool is None:  # DB may still be starting; compose also waits for migrate
        try:
            app.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
            await app.refresh_devices()
            app.state.db = True
        except (asyncpg.PostgresError, OSError) as exc:
            log.warning("db not ready", extra={"error": str(exc)})
            await asyncio.sleep(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 2)
    log.info("devices loaded", extra={"count": len(app.devices)})

    server = uvicorn.Server(
        uvicorn.Config(
            build_api(app), host="0.0.0.0", port=settings.ingest_http_port, log_level="warning"
        )
    )
    http_task = asyncio.create_task(server.serve())
    cache_task = asyncio.create_task(app.cache_loop())
    writer_task = asyncio.create_task(app.writer_loop())
    sub_task = asyncio.create_task(app.subscriber_loop())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    await stop.wait()

    log.info("shutting down: closing subscription, draining queue")
    sub_task.cancel()
    await asyncio.gather(sub_task, return_exceptions=True)
    await app.queue.put(_STOP)
    await writer_task
    cache_task.cancel()
    server.should_exit = True
    await http_task
    await app.publisher.close()
    assert app.pool is not None
    await app.pool.close()
    log.info("stopped")
