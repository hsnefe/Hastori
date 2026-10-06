"""RabbitMQ publishing for the outbox relay (topic exchange, publisher confirms, mandatory)."""

import logging
import time
from collections.abc import Sequence
from typing import Any

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractRobustConnection

from hastori_common.messaging import declare_topology
from hastori_ingestion.metrics import PUBLISH_FAILURES

log = logging.getLogger("ingestion.publisher")

RECONNECT_COOLDOWN_S = 5.0
# A broker under a memory/disk alarm blocks publishers instead of refusing them; without a
# timeout the relay would wait for it indefinitely.
PUBLISH_TIMEOUT_S = 5.0


class Publisher:
    def __init__(self, url: str) -> None:
        self._url = url
        self._conn: AbstractRobustConnection | None = None
        self._channel: Any = None
        self._exchange: AbstractExchange | None = None
        self._last_attempt = 0.0

    @property
    def connected(self) -> bool:
        """Really connected now. A robust connection is only `is_closed` after an explicit
        close(); while it is reconnecting after a broker outage its `connected` event is clear."""
        conn = self._conn
        return (
            conn is not None
            and conn.connected.is_set()
            and self._channel is not None
            and not self._channel.is_closed
            and self._exchange is not None
        )

    async def _ensure(self) -> AbstractExchange | None:
        if self.connected:
            return self._exchange
        if self._conn is not None and not self._conn.is_closed:
            return None  # the robust connection is reconnecting by itself; do not open a second
        now = time.monotonic()
        if now - self._last_attempt < RECONNECT_COOLDOWN_S:
            return None
        self._last_attempt = now
        try:
            self._conn = await aio_pika.connect_robust(self._url, timeout=5)
            self._channel = await self._conn.channel(publisher_confirms=True)
            self._exchange, _ = await declare_topology(self._channel)
            log.info("rabbitmq connected")
        except Exception as exc:
            # PRECONDITION_FAILED here = the queue exists with older arguments:
            # `make reset-alarm-queue`, then restart ingestion.
            log.warning("rabbitmq unavailable", extra={"error": str(exc)})
            self._conn = None
            self._channel = None
            self._exchange = None
        return self._exchange

    async def publish_rows(self, rows: Sequence[Any]) -> list[int]:
        """Publish outbox rows in order; returns the ids that the broker confirmed.

        Stops at the first failure so ordering is kept and the rest stays in the outbox.
        """
        exchange = await self._ensure()
        if exchange is None or not self.connected:
            PUBLISH_FAILURES.inc(len(rows))
            return []
        done: list[int] = []
        for row in rows:
            msg = aio_pika.Message(
                row["body"].encode(),
                message_id=str(row["message_id"]),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            try:
                await exchange.publish(
                    msg,
                    routing_key=row["routing_key"],
                    mandatory=True,
                    timeout=PUBLISH_TIMEOUT_S,
                )
            except Exception as exc:
                PUBLISH_FAILURES.inc()
                log.warning(
                    "publish failed",
                    extra={"message_id": str(row["message_id"]), "error": str(exc)},
                )
                break
            done.append(row["id"])
        return done

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed:
            await self._conn.close()
