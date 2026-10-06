# Hastori

Industrial telemetry platform. Day 1 covers the infrastructure: a simulator publishes
measurements from 7 devices every 2 seconds over MQTT (TLS + ACL) to an ingestion service,
which writes them to TimescaleDB and publishes them to RabbitMQ. Everything starts with
one `docker compose up`.

## Hızlı başlangıç

_(Paket 7'de doldurulacak.)_
