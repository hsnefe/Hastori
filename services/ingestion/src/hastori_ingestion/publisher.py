"""RabbitMQ publishing (topic exchange, publisher confirms). Failures are counted, never fatal."""

import json
import logging
import time

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractRobustConnection

from hastori_ingestion.metrics import PUBLISH_FAILURES
from hastori_ingestion.parsing import Telemetry

log = logging.getLogger("ingestion.publisher")

EXCHANGE = "hastori.telemetry"
RECONNECT_COOLDOWN_S = 5.0


class Publisher:
    def __init__(self, url: str) -> None:
        self._url = url
        self._conn: AbstractRobustConnection | None = None
        self._exchange: AbstractExchange | None = None
        self._last_attempt = 0.0

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.is_closed and self._exchange is not None

    async def _ensure(self) -> AbstractExchange | None:
        if self.connected:
            return self._exchange
        now = time.monotonic()
        if now - self._last_attempt < RECONNECT_COOLDOWN_S:
            return None
        self._last_attempt = now
        try:
            self._conn = await aio_pika.connect_robust(self._url, timeout=5)
            channel = await self._conn.channel(publisher_confirms=True)
            self._exchange = await channel.declare_exchange(
                EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
            )
            log.info("rabbitmq connected")
        except Exception as exc:
            log.warning("rabbitmq unavailable", extra={"error": str(exc)})
            self._conn = None
            self._exchange = None
        return self._exchange

    async def publish(self, items: list[Telemetry]) -> None:
        exchange = await self._ensure()
        if exchange is None:
            PUBLISH_FAILURES.inc(len(items))
            return
        for t in items:
            body = json.dumps(
                {
                    "message_id": str(t.message_id),
                    "site_id": str(t.site_id),
                    "device_id": str(t.device_id),
                    "ts": t.ts,
                    "metrics": t.metrics,
                }
            ).encode()
            msg = aio_pika.Message(
                body,
                message_id=str(t.message_id),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            try:
                await exchange.publish(msg, routing_key=f"telemetry.{t.site_id}.{t.device_id}")
            except Exception as exc:
                PUBLISH_FAILURES.inc()
                log.warning(
                    "publish failed",
                    extra={"message_id": str(t.message_id), "error": str(exc)},
                )

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed:
            await self._conn.close()
