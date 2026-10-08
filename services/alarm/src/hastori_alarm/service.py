"""Alarm service wiring.

RabbitMQ (`alarm.telemetry`, manual acks, one active consumer) -> state machine (engine.py) ->
alarm rows in the database -> Redis event -> ack.

Delivery guarantees:
- A message is acked only after the database write of its transitions committed. If the service
  dies in between, the broker redelivers it; the engine drops a reading it has already seen and
  every write is conditional, so nothing is opened or closed twice.
- At start the last minutes are replayed from `measurements` into a fresh engine. The state of a
  rule therefore never depends on memory that was lost: it is a function of stored data.
- The alarm table is the source of truth. Redis events are hints for live screens.
"""

import asyncio
import contextlib
import logging
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import aio_pika
import asyncpg
import uvicorn
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue, AbstractRobustConnection
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis

from hastori_alarm import metrics
from hastori_alarm.engine import Engine, Rule, Sample, Transition
from hastori_alarm.store import OpenAlarm, Store
from hastori_common.dberrors import PERMANENT_DB_ERRORS, RETRYABLE_DB_ERRORS
from hastori_common.events import AlarmEvent, channel
from hastori_common.logging import configure_logging
from hastori_common.messaging import declare_topology
from hastori_common.serve import Server
from hastori_common.settings import Settings, get_settings

log = logging.getLogger("alarm")

# What the engine is given at start: enough for the longest pending duration plus margin, and for
# the whole window of the longest reactive rule.
REPLAY_MIN_S = 900.0
PREFETCH = 200
LIVE_WITHIN_S = 60.0  # a reading this close to the wall clock is live, not queued
RELOAD_INTERVAL_S = 60.0
LISTEN_CHECK_S = 5.0
# A second replica waits as a standby (single active consumer) and may become active hours after
# it started. If nothing was processed for this long, the engine may be missing everything the
# other replica did: rebuild it from the database before the first message.
STANDBY_RESYNC_AFTER_S = 120.0
BACKOFF_MAX_S = 30.0
DB_COMMAND_TIMEOUT_S = 30.0
REDIS_TIMEOUT_S = 2.0
NOTIFY_CHANNELS = ("alarm_rules_changed", "devices_changed")

T = TypeVar("T")


class Stopping(Exception):
    """Shutdown deadline passed while the database was still unavailable."""


class TelemetryEvent(BaseModel):
    """The body ingestion's outbox relay publishes."""

    message_id: uuid.UUID
    site_id: uuid.UUID
    device_id: uuid.UUID
    ts: float
    metrics: dict[str, float]


class State:
    """Readiness flags shared with the HTTP endpoints."""

    db = False
    redis = False


class AlarmService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = Engine()
        # Live messages, reloads and shutdown all change alarms; one at a time keeps the order of
        # transitions per rule the order of the data.
        self.lock = asyncio.Lock()
        self.pool: asyncpg.Pool | None = None
        self.store: Store | None = None
        self.redis: Redis | None = None
        self.site_of_device: dict[uuid.UUID, uuid.UUID] = {}
        self.state = State()
        self.stop_event = asyncio.Event()
        self.reload_event = asyncio.Event()
        self.deadline: float | None = None
        self.tasks: list[asyncio.Task[Any]] = []
        self._conn: AbstractRobustConnection | None = None
        self._channel: Any = None
        self._last_handled = time.monotonic()
        self._dirty = False  # the engine moved ahead of the database: rebuild it

    # -- database ---------------------------------------------------------------------------
    async def _retry(self, op: Callable[[], Awaitable[T]]) -> T:
        """Run a database operation until it succeeds. Only an error that is a property of the
        data (PERMANENT_DB_ERRORS) propagates; during shutdown it gives up at the deadline and
        leaves the message un-acked for redelivery."""
        backoff = 0.5
        while True:
            try:
                result = await op()
                self.state.db = True
                return result
            except PERMANENT_DB_ERRORS:
                raise
            except RETRYABLE_DB_ERRORS as exc:
                self.state.db = False
                if self.deadline is not None and time.monotonic() > self.deadline:
                    raise Stopping from exc
                log.warning("db operation failed", extra={"error": str(exc), "retry_in_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)

    @property
    def _store(self) -> Store:
        assert self.store is not None
        return self.store

    # -- events -----------------------------------------------------------------------------
    async def publish(self, event: AlarmEvent) -> None:
        """Best effort: the alarm is already committed, and screens re-read it over REST."""
        if self.redis is None:
            return
        try:
            await asyncio.wait_for(
                self.redis.publish(channel(event.site_id), event.model_dump_json()),
                REDIS_TIMEOUT_S,
            )
            self.state.redis = True
        except Exception as exc:
            self.state.redis = False
            metrics.EVENT_PUBLISH_FAILURES.inc()
            log.warning("alarm event not published", extra={"error": str(exc)})

    # -- transitions ------------------------------------------------------------------------
    async def apply(self, t: Transition, *, suppress_up_to: float | None = None) -> None:
        """Write one transition and announce it.

        `suppress_up_to`: during the start-up replay, transitions at or before the rule's latest
        recorded alarm event were already written by a previous run; the engine has to see them
        to reach the right state, the database must not.
        """
        if suppress_up_to is not None and t.ts <= suppress_up_to:
            return
        rule, store = t.rule, self._store
        site_id = self.site_of_device.get(rule.device_id)
        if site_id is None:
            log.error("rule of an unknown device", extra={"rule_id": str(rule.id)})
            return
        if t.kind == "opened":
            alarm_id = await self._retry(lambda: store.open_alarm(rule, t.ts, t.peak))
            if alarm_id is None:
                return  # already open (adopted): nothing new happened
            event = AlarmEvent(
                type="alarm.opened",
                alarm_id=alarm_id,
                rule_id=rule.id,
                rule_name=rule.name,
                device_id=rule.device_id,
                site_id=site_id,
                severity=rule.severity,  # type: ignore[arg-type]
                state="active",
                value=t.value,
                ts=t.ts,
            )
        else:
            alarm_id = await self._retry(lambda: store.clear_alarm(rule.id, t.ts, t.peak))
            if alarm_id is None:
                return  # nothing was open
            event = AlarmEvent(
                type="alarm.cleared",
                alarm_id=alarm_id,
                rule_id=rule.id,
                rule_name=rule.name,
                device_id=rule.device_id,
                site_id=site_id,
                severity=rule.severity,  # type: ignore[arg-type]
                state="cleared",
                value=t.peak,
                ts=t.ts,
            )
        metrics.TRANSITIONS.labels(to=t.kind).inc()
        log.info(
            "alarm %s",
            t.kind,
            extra={
                "alarm_id": str(alarm_id),
                "rule": rule.name,
                "device_id": str(rule.device_id),
                "value": t.value,
                "peak": t.peak,
                "forced": t.forced,
            },
        )
        await self.publish(event)
        metrics.ALARMS_OPEN.set(self.engine.open_count())

    async def _close_orphan(self, alarm: OpenAlarm, now: float) -> None:
        """An alarm that is open in the database for a rule that no longer applies (deleted,
        disabled, its device deactivated, possibly while this service was down)."""
        cleared = await self._retry(
            lambda: self._store.clear_alarm(alarm.rule_id, now, alarm.peak or 0.0)
        )
        if cleared is None:
            return
        metrics.TRANSITIONS.labels(to="cleared").inc()
        log.info("alarm closed: its rule no longer applies", extra={"alarm_id": str(alarm.id)})
        await self.publish(
            AlarmEvent(
                type="alarm.cleared",
                alarm_id=alarm.id,
                rule_id=alarm.rule_id,
                rule_name=alarm.rule_name,
                device_id=alarm.device_id,
                site_id=alarm.site_id,
                severity=alarm.severity,  # type: ignore[arg-type]
                state="cleared",
                value=alarm.peak,
                ts=now,
            )
        )

    # -- start-up and reloads ---------------------------------------------------------------
    async def startup(self) -> None:
        catalog = await self._retry(self._store.load_catalog)
        self.site_of_device = catalog.site_of_device
        now = time.time()
        self.engine.set_rules(catalog.rules, now)
        enabled = {r.id for r in catalog.rules}
        for alarm in await self._retry(self._store.open_alarms):
            if alarm.rule_id in enabled:
                self.engine.adopt_open(alarm.rule_id, alarm.opened_at, alarm.peak)
            else:
                await self._close_orphan(alarm, now)
        watermarks = await self._retry(self._store.watermarks)
        await self.replay(catalog.rules, watermarks)
        metrics.ALARMS_OPEN.set(self.engine.open_count())
        log.info("alarm service ready", extra={"rules": len(catalog.rules)})

    async def replay(self, rules: list[Rule], watermarks: dict[uuid.UUID, float]) -> None:
        if not rules:
            return
        # A pending count that began before the restart must be rebuilt: look back over the longest
        # rule duration or reactive window, not just the default.
        longest = max(max(r.window_s or 0, r.duration_s) for r in rules)
        lookback = max(REPLAY_MIN_S, longest + 60.0)
        devices = sorted({r.device_id for r in rules})
        since = time.time() - lookback
        samples = await self._retry(lambda: self._store.samples(devices, since))
        for sample in samples:
            for t in self.engine.feed(sample):
                await self.apply(t, suppress_up_to=watermarks.get(t.rule.id))
        # What the replay covered (and everything older) is history: a queued message from before
        # the restart is a backlog to drop, not a device clock that was set back.
        self.engine.history_floor(since)
        log.info("replayed", extra={"samples": len(samples), "lookback_s": lookback})

    async def resync(self) -> None:
        """Rebuild the engine from the database (rules, open alarms, a replay of recent samples)."""
        async with self.lock:
            self.engine = Engine()
            await self.startup()
            self._dirty = False
        log.info("engine rebuilt from the database")

    async def reload(self) -> None:
        """Pick up changed rules and devices (a notification, or the periodic safety net)."""
        async with self.lock:
            catalog = await self._retry(self._store.load_catalog)
            self.site_of_device = catalog.site_of_device
            now = time.time()
            for t in self.engine.set_rules(catalog.rules, now):
                await self.apply(t)
            enabled = {r.id for r in catalog.rules}
            for alarm in await self._retry(self._store.open_alarms):
                if alarm.rule_id not in enabled:
                    await self._close_orphan(alarm, now)
            metrics.ALARMS_OPEN.set(self.engine.open_count())
        if self.redis is not None:
            try:
                await asyncio.wait_for(self.redis.ping(), REDIS_TIMEOUT_S)
                self.state.redis = True
            except Exception:
                self.state.redis = False

    async def reload_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.reload_event.wait(), RELOAD_INTERVAL_S)
            await asyncio.sleep(0.2)  # a burst of notifications (a re-seed) is one reload
            self.reload_event.clear()
            try:
                await self.reload()
            except Stopping:
                return
            except Exception as exc:
                log.warning("reload failed", extra={"error": str(exc)})

    async def listen_loop(self, dsn: str) -> None:
        """Keep a connection LISTENing for rule and device changes; reconnect when it drops."""
        listener: asyncpg.Connection | None = None
        try:
            while True:
                try:
                    if listener is None or listener.is_closed():
                        listener = await asyncpg.connect(dsn, timeout=10)
                        for name in NOTIFY_CHANNELS:
                            await listener.add_listener(name, lambda *_: self.reload_event.set())
                        self.reload_event.set()  # whatever changed while we were not listening
                    await asyncio.wait_for(listener.execute("SELECT 1"), 5)  # a dead TCP link
                except Exception as exc:
                    if listener is not None:
                        with contextlib.suppress(Exception):
                            await listener.close(timeout=2)
                    listener = None
                    log.warning("rule listener unavailable", extra={"error": str(exc)})
                await asyncio.sleep(LISTEN_CHECK_S)
        finally:
            if listener is not None and not listener.is_closed():
                with contextlib.suppress(Exception):
                    await asyncio.shield(listener.close(timeout=2))

    # -- RabbitMQ ---------------------------------------------------------------------------
    @property
    def rabbitmq_ready(self) -> bool:
        conn, ch = self._conn, self._channel
        return conn is not None and conn.connected.is_set() and ch is not None and not ch.is_closed

    async def handle(self, message: AbstractIncomingMessage) -> None:
        try:
            event = TelemetryEvent.model_validate_json(message.body)
        except ValidationError as exc:
            metrics.REJECTED.labels(reason="invalid_event").inc()
            log.warning(
                "message rejected", extra={"reason": "invalid_event", "error": str(exc)[:200]}
            )
            await self._settle(message.reject(requeue=False))
            return
        sample = Sample(event.device_id, event.ts, event.metrics)
        try:
            if self._dirty or time.monotonic() - self._last_handled > STANDBY_RESYNC_AFTER_S:
                await self.resync()
        except Stopping:
            return
        except PERMANENT_DB_ERRORS as exc:
            self._dirty = False
            log.error("rebuilding the engine failed for good", extra={"error": str(exc)})
        if abs(time.time() - event.ts) <= LIVE_WITHIN_S:
            self.engine.mark_live(event.device_id)
        try:
            async with self.lock:
                jumps = self.engine.clock_jumps
                transitions = self.engine.feed(sample)
                if self.engine.clock_jumps > jumps:
                    metrics.CLOCK_JUMPS.inc(self.engine.clock_jumps - jumps)
                    log.warning("device clock set back", extra={"device_id": str(event.device_id)})
                for t in transitions:
                    await self.apply(t)
        except Stopping:
            return  # un-acked: redelivered after the restart, and the replay rebuilds the state
        except PERMANENT_DB_ERRORS as exc:
            # The engine already moved on, the database did not take the transition: rebuild the
            # engine from the database before the next message, or an alarm would be "open" in
            # memory only and never announced.
            self._dirty = True
            metrics.REJECTED.labels(reason="db_rejected").inc()
            log.error(
                "message dropped by database",
                extra={"message_id": str(event.message_id), "error": str(exc)},
            )
            await self._settle(message.reject(requeue=False))
            return
        except Exception:
            self._dirty = True  # same: the redelivery must meet an engine that matches the data
            raise
        self._last_handled = time.monotonic()
        metrics.MESSAGES.inc()
        metrics.EVAL_LAG.observe(max(0.0, time.time() - event.ts))
        await self._settle(message.ack())

    @staticmethod
    async def _settle(op: Awaitable[Any]) -> None:
        """An ack or reject on a connection that was replaced meanwhile fails; the broker
        redelivers the message on the new connection and the engine drops the duplicate."""
        try:
            await op
        except Exception as exc:
            log.warning("ack failed", extra={"error": str(exc)})

    async def _consume(self, queue: AbstractQueue) -> None:
        """Process messages one at a time, in order. Only the wait for the next message is
        interrupted by shutdown: a message that is being processed is finished."""
        async with queue.iterator() as messages:
            while not self.stop_event.is_set():
                nxt = asyncio.ensure_future(messages.__anext__())
                stop = asyncio.ensure_future(self.stop_event.wait())
                done, _ = await asyncio.wait({nxt, stop}, return_when=asyncio.FIRST_COMPLETED)
                if nxt not in done:
                    nxt.cancel()
                    await asyncio.gather(nxt, return_exceptions=True)
                    return
                stop.cancel()
                await self.handle(nxt.result())

    async def consumer_loop(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            conn: AbstractRobustConnection | None = None
            try:
                conn = await aio_pika.connect_robust(self.settings.rabbitmq_url, timeout=10)
                channel_ = await conn.channel()
                await channel_.set_qos(prefetch_count=PREFETCH)
                # PRECONDITION_FAILED here = the queue exists with older arguments:
                # `make reset-alarm-queue`, then restart ingestion and this service.
                _, queue = await declare_topology(channel_)
                self._conn, self._channel = conn, channel_
                metrics.RECONNECTS.inc()
                backoff = 1.0
                log.info("consuming", extra={"queue": queue.name})
                await self._consume(queue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("rabbitmq error", extra={"error": str(exc), "retry_in_s": backoff})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
            finally:
                self._conn = self._channel = None
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()


def build_api(app: AlarmService) -> FastAPI:
    api = FastAPI(title="Hastori alarm service")

    @api.get("/healthz")
    def healthz(response: Response) -> dict[str, str]:
        """Liveness: every background loop is still running."""
        dead = [t.get_name() for t in app.tasks if t.done() and not app.stop_event.is_set()]
        if dead:
            response.status_code = 503
            return {"status": "failing", "dead": ",".join(dead)}
        return {"status": "ok"}

    @api.get("/readyz")
    def readyz(response: Response) -> dict[str, bool]:
        status = {
            "rabbitmq": app.rabbitmq_ready,
            "db": app.state.db,
            "redis": app.state.redis,
        }
        if not all(status.values()):
            response.status_code = 503
        return status

    @api.get("/metrics")
    def prometheus() -> Response:
        return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return api


async def run() -> None:
    settings = get_settings(strict=("database_url", "rabbitmq_url", "redis_url"))
    configure_logging(settings.log_level)
    app = AlarmService(settings)
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    backoff = 1.0
    while app.pool is None:  # the database may still be starting; compose also waits for migrate
        try:
            app.pool = await asyncpg.create_pool(
                dsn,
                min_size=1,
                max_size=4,
                command_timeout=DB_COMMAND_TIMEOUT_S,
                server_settings={"idle_in_transaction_session_timeout": "60000"},
            )
            await app.pool.fetchval("SELECT 1")
            app.state.db = True
        except (asyncpg.PostgresError, OSError) as exc:
            log.warning("db not ready", extra={"error": str(exc)})
            if app.pool is not None:
                await app.pool.close()
                app.pool = None
            await asyncio.sleep(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 2)
    app.store = Store(app.pool)
    app.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await asyncio.wait_for(app.redis.ping(), 5)
        app.state.redis = True
    except Exception as exc:
        log.warning("redis not ready", extra={"error": str(exc)})

    await app.startup()

    server = Server(
        uvicorn.Config(
            build_api(app), host="0.0.0.0", port=settings.alarm_http_port, log_level="warning"
        )
    )
    app.tasks = [
        asyncio.create_task(server.serve(), name="http"),
        asyncio.create_task(app.listen_loop(dsn), name="listener"),
        asyncio.create_task(app.reload_loop(), name="reload"),
        asyncio.create_task(app.consumer_loop(), name="consumer"),
    ]
    http_task, listen_task, reload_task, consumer_task = app.tasks

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, app.stop_event.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(app.stop_event.set))

    # If a loop dies the process would keep looking alive while nothing is evaluated: exit
    # non-zero so the restart policy applies (the broker redelivers what was not acked).
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

    log.info("shutting down")
    app.deadline = time.monotonic() + settings.alarm_shutdown_deadline_s
    await consumer_task  # finishes the message in hand, stops consuming, closes the connection
    for task in (listen_task, reload_task):
        task.cancel()
    await asyncio.gather(listen_task, reload_task, return_exceptions=True)
    server.should_exit = True
    await http_task
    if app.redis is not None:
        await app.redis.aclose()
    assert app.pool is not None
    await app.pool.close()
    log.info("stopped")
