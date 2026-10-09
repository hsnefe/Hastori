# ADR 0002: RabbitMQ, not Kafka, between ingestion and the alarm service

Status: accepted (day 1, revised day 2)

## Context

Ingestion must hand every committed reading to the alarm service, once in order for a device,
without losing any when either side restarts, and without a bad message blocking the rest.
Devices talk MQTT to the broker in front of ingestion; that part is not in question.

## Decision

Ingestion writes the reading and an outbox row in one transaction, a relay publishes the outbox to
a RabbitMQ topic exchange (publisher confirms on), and the alarm service consumes one durable
queue with a single active consumer. A message it cannot parse is rejected to a dead-letter queue.

## Why

- **The shape of the work is a queue, not a log.** One consumer group, no replay of history from
  the broker (the alarm service replays the last minutes from the database on start), no
  partition key to choose.
- **Dead-lettering is built in.** A poisoned message goes to `alarm.telemetry.dlq` and stops
  being redelivered; the stream keeps flowing.
- **Single active consumer** gives ordered processing and a hot standby with one queue argument.
  Kafka's equivalent is partition assignment, which is more to operate for one consumer.
- **Footprint.** One small container; Kafka plus its controller is several times the memory on a
  2-vCPU demo machine.
- The management plugin and `rabbitmq_prometheus` give queue depth and DLQ growth for the alerts
  without extra exporters.

## Consequences

- Throughput is capped by one consumer (demo: about 3.5 messages a second, a large margin). Scale
  means sharding by device, which is a different topology (risk G7).
- Order is only per queue: an unrelated redelivery can arrive late, and the alarm engine works in
  the readings' own time, not arrival time, for that reason.
- RabbitMQ refuses to redeclare an existing queue with different arguments; `make
  reset-alarm-queue` exists for the one time that mattered.
- Kafka would be the answer if several independent consumers needed the same history, or replay
  of weeks from the broker. Neither is a requirement here.
