"""RabbitMQ topology shared by the producer (ingestion) and consumers (alarm service).

Both sides declare the same definitions, so the queue exists before the first message is
published: a topic exchange drops messages that have no bound queue.
"""

from typing import Any

EXCHANGE = "hastori.telemetry"
ALARM_QUEUE = "alarm.telemetry"
ALARM_BINDING = "telemetry.#"

# Bounded so a stopped consumer cannot fill the broker: oldest messages drop first.
ALARM_QUEUE_ARGS: dict[str, Any] = {
    "x-max-length": 200_000,
    "x-overflow": "drop-head",
    "x-message-ttl": 3_600_000,
}


def routing_key(site_id: object, device_id: object) -> str:
    return f"telemetry.{site_id}.{device_id}"
