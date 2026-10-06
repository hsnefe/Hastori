"""RabbitMQ topology shared by the producer (ingestion) and consumers (alarm service).

Every side declares the same definitions through `declare_topology`, so the queue exists before
the first message is published (a topic exchange drops messages that have no bound queue) and a
drifting definition fails loudly instead of silently diverging.
"""

from typing import Any

import aio_pika

EXCHANGE = "hastori.telemetry"
ALARM_QUEUE = "alarm.telemetry"
ALARM_BINDING = "telemetry.#"

DLX = "hastori.dlx"
DLQ = "alarm.telemetry.dlq"
DLQ_MAX_LENGTH = 10_000
DLQ_TTL_MS = 7 * 24 * 3_600_000

# Bounded so a stopped consumer cannot fill the broker: oldest messages drop first. A message
# that is rejected, expires or is dropped for length goes to the dead-letter exchange, so nothing
# disappears without a trace (the DLQ itself is bounded too).
ALARM_QUEUE_ARGS: dict[str, Any] = {
    "x-max-length": 200_000,
    "x-overflow": "drop-head",
    "x-message-ttl": 3_600_000,
    "x-dead-letter-exchange": DLX,
    # The alarm state machine lives in the memory of one process and needs messages in order:
    # a second replica waits and takes over if the first goes away.
    "x-single-active-consumer": True,
}
DLQ_ARGS: dict[str, Any] = {
    "x-max-length": DLQ_MAX_LENGTH,
    "x-overflow": "drop-head",
    "x-message-ttl": DLQ_TTL_MS,
}


def routing_key(site_id: object, device_id: object) -> str:
    return f"telemetry.{site_id}.{device_id}"


async def declare_topology(channel: Any) -> tuple[Any, Any]:
    """Declare exchange, alarm queue, dead-letter exchange and queue; returns (exchange, queue).

    Changing a queue argument makes RabbitMQ refuse the declaration of an existing queue
    (PRECONDITION_FAILED): delete the queue once (`make reset-alarm-queue`) and restart.
    """
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    dlx = await channel.declare_exchange(DLX, aio_pika.ExchangeType.FANOUT, durable=True)
    dlq = await channel.declare_queue(DLQ, durable=True, arguments=dict(DLQ_ARGS))
    await dlq.bind(dlx)
    queue = await channel.declare_queue(ALARM_QUEUE, durable=True, arguments=dict(ALARM_QUEUE_ARGS))
    await queue.bind(exchange, routing_key=ALARM_BINDING)
    return exchange, queue
