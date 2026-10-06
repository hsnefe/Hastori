# Hastori

Industrial telemetry platform. Day 1 is the infrastructure: a simulator publishes
measurements from 7 devices every 2 seconds over MQTT (TLS + per-device ACL) to an ingestion
service, which writes them to TimescaleDB and publishes them to RabbitMQ. API, alarms and the
dashboard come on days 2-3.

```
simulator --MQTT 5, QoS 1, TLS--> mosquitto --$share--> ingestion --> TimescaleDB
 (7 devices, own credentials)                              |
                                                           +--> RabbitMQ (hastori.telemetry)
```

`seed/demo.yaml` is the single source of truth: the database seed, the Mosquitto password/ACL
files and the simulator device list are all generated from it.

## Hızlı başlangıç

Requirements: Docker + Compose, [uv](https://docs.astral.sh/uv/), Git. `make` is optional on
Windows (`winget install ezwinports.make`); PowerShell equivalents are below.

```bash
git clone <repo-url> hastori && cd hastori
make up          # .env, TLS certs, MQTT users/ACL, then timescaledb + mosquitto + rabbitmq + migrate + ingestion
make seed        # demo org, 2 sites, 7 devices, 3 users, 2 alarm rules (idempotent)
make simulate    # start the field simulator
make smoke       # automated day-1 checks
make fault DEVICE=izmir-komp-1 KIND=overheat   # trigger an overheat scenario
```

| make target     | PowerShell equivalent |
|-----------------|-----------------------|
| `make env`      | `if (-not (Test-Path .env)) { Copy-Item .env.example .env }` |
| `make certs`    | `uv run python scripts/gen_certs.py` |
| `make mqtt-auth`| `uv run python scripts/gen_mqtt_auth.py` |
| `make up`       | env + certs + mqtt-auth above, then `docker compose up -d --build --wait` |
| `make down`     | `docker compose --profile sim down` |
| `make logs`     | `docker compose --profile sim logs -f --tail=100` |
| `make migrate`  | `docker compose run --rm migrate` |
| `make seed`     | `uv run python scripts/seed.py` |
| `make simulate` | `docker compose --profile sim up -d --build simulator` |
| `make fault`    | `uv run python scripts/fault.py izmir-komp-1 overheat 40` |
| `make smoke`    | `uv run python scripts/smoke.py` |
| `make test`     | `uv run pytest` |
| `make lint`     | `uv run ruff check .; uv run ruff format --check .; uv run mypy packages services scripts` |

Fault kinds: `overheat`, `spike`, `compensation_failure`, `offline`.

### Endpoints (all bound to 127.0.0.1)

| Service | URL |
|---------|-----|
| Ingestion health / readiness / metrics | `http://127.0.0.1:8001/healthz`, `/readyz`, `/metrics` |
| Simulator fault control | `http://127.0.0.1:8002/faults` |
| RabbitMQ management | `http://127.0.0.1:15672` (credentials in `.env`) |
| TimescaleDB | `127.0.0.1:5432` |
| MQTT (TLS only) | `127.0.0.1:8883` (CA: `infra/mosquitto/certs/ca.crt`) |

## Design notes

- **Duplicates**: MQTT QoS 1 is at-least-once. `measurements` has a unique index on
  `(device_id, metric, time)` and ingestion uses `ON CONFLICT DO NOTHING`.
- **Scaling**: ingestion uses an MQTT 5 shared subscription (`$share/ingestion/...`); a second
  replica splits the load instead of double-writing.
- **Write order**: database first, then RabbitMQ. Publish failures are counted
  (`ingest_publish_failures_total`) and logged; measurements are never lost to a broker outage.
- **Device credentials** are not stored: `HMAC-SHA256(MQTT_DEVICE_SECRET, device_id)`.

## Known limits

- MQTT acks are sent when a message is received, not after the DB write. On a crash, up to
  about 1 second of queued data can be lost.
- Mosquitto TLS uses a local demo CA (`make certs`); not for production.

## What I would do differently in production

- **Transactional outbox** between the database and RabbitMQ instead of "DB, then publish with
  failure counting": write an outbox row in the same transaction and relay it, so downstream
  consumers can never miss an event.
- Ack MQTT messages only after the commit (manual acks / MQTT 5 flow control).
- Real PKI for broker and device certificates, secrets from a vault rather than `.env`.
- Redis, Caddy, Grafana and CI are deliberately deferred to days 2-4.
