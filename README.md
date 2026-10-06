# Hastori

Industrial telemetry platform. Day 1 is the infrastructure: a simulator publishes
measurements from 7 devices every 2 seconds over MQTT (TLS + per-device ACL) to an ingestion
service, which writes them to TimescaleDB and publishes them to RabbitMQ. API, alarms and the
dashboard come on days 2-3.

```
simulator --MQTT 5, QoS 1, TLS--> mosquitto --$share--> ingestion --+--> TimescaleDB (measurements + outbox)
 (7 devices, own credentials)                                       |
                                                                    +--> outbox relay --> RabbitMQ (hastori.telemetry)
```

`seed/demo.yaml` is the single source of truth: the database seed, the Mosquitto password/ACL
files and the simulator device list are all generated from it.

## Hızlı başlangıç

Requirements: Docker + Compose, [uv](https://docs.astral.sh/uv/), Git. `make` is optional on
Windows (`winget install ezwinports.make`); PowerShell equivalents are below.

```bash
git clone <repo-url> hastori && cd hastori
make up          # .env with random secrets, TLS certs, MQTT users/ACL, then timescaledb + mosquitto + rabbitmq + migrate + ingestion
make seed        # demo org, 2 sites, 7 devices, 3 users, 2 alarm rules (idempotent)
make simulate    # start the field simulator
make smoke       # automated day-1 checks
make fault DEVICE=izmir-komp-1 KIND=overheat   # trigger an overheat scenario
```

Demo logins (seeded): `admin@demo.hastori.local` (system admin), `izmir.admin@demo.hastori.local`
(site admin), `izmir.izleyici@demo.hastori.local` (viewer); passwords are the `SEED_*` values in
`.env` (`admin_demo_pw`, `siteadmin_demo_pw`, `viewer_demo_pw` by default). All other secrets in
`.env` are generated randomly by `make env`.

| make target      | PowerShell equivalent |
|------------------|-----------------------|
| `make env`       | `uv run python scripts/gen_env.py` (never overwrites an existing `.env`) |
| `make certs`     | `uv run python scripts/gen_certs.py` (renews the server certificate when < 30 days are left) |
| `make mqtt-auth` | `uv run python scripts/gen_mqtt_auth.py` |
| `make up`        | env + certs + mqtt-auth above, then `docker compose up -d --build --wait` |
| `make down`      | `docker compose --profile sim down` |
| `make logs`      | `docker compose --profile sim logs -f --tail=100` |
| `make migrate`   | `docker compose run --rm migrate` |
| `make seed`      | `uv run python scripts/seed.py` |
| `make seed-reset`| `uv run python scripts/seed.py --reset` (YAML wins: re-hash passwords, restore rules) |
| `make simulate`  | `docker compose --profile sim up -d --build simulator` |
| `make fault`     | `uv run python scripts/fault.py izmir-komp-1 overheat 40` |
| `make smoke`     | `uv run python scripts/smoke.py` |
| `make smoke-quick` | `uv run python scripts/smoke.py --no-fault` |
| `make resilience`| `uv run python scripts/resilience.py` (about 5 minutes) |
| `make test`      | `uv run pytest` |
| `make lint`      | `uv run ruff check .; uv run ruff format --check .; uv run mypy packages services scripts` |

Fault kinds: `overheat`, `spike`, `compensation_failure`, `offline`.
`make smoke` injects a real 40 s overheat into `izmir-komp-1` (and clears it afterwards); use
`make smoke-quick` whenever alarms are being tested.

### Endpoints (all bound to 127.0.0.1)

| Service | URL |
|---------|-----|
| Ingestion health / readiness / metrics | `http://127.0.0.1:8001/healthz`, `/readyz`, `/metrics` |
| Simulator fault control | `http://127.0.0.1:8002/faults` (`Authorization: Bearer $SIM_CONTROL_TOKEN`) |
| RabbitMQ management | `http://127.0.0.1:15672` (credentials in `.env`) |
| TimescaleDB | `127.0.0.1:5432` |
| MQTT (TLS only) | `127.0.0.1:8883` (CA: `infra/mosquitto/certs/ca.crt`) |

The ingestion endpoints have no authentication; they only expose counters and readiness. Do not
widen the `127.0.0.1` port bindings without putting the gateway (day 4) in front.

## Design notes

- **Delivery**: a message is acked to the broker (MQTT 5 manual ack) only after its database
  transaction committed. After a crash or restart the broker redelivers it, and the unique index
  on `(device_id, metric, time)` plus `ON CONFLICT DO NOTHING` absorb the duplicate.
- **Outbox**: the RabbitMQ event is inserted into an `outbox` table in the same transaction as the
  measurements; a relay publishes it with publisher confirms and then deletes it. An event cannot
  fall between "committed" and "published". Events older than 1 hour are dropped and counted
  (`ingest_outbox_expired_total`); `ingest_outbox_depth` shows the backlog.
- **Poison messages**: transient database errors (connection, restart, deadlock) are retried
  with backoff; a permanent error (for example a foreign-key violation) switches that batch to
  row-by-row commits so only the offending message is dropped (`reason="db_rejected"`).
- **Durable queue**: ingestion declares `alarm.telemetry` (bound to `telemetry.#`, bounded to
  200 000 messages / 1 h) next to the exchange, because a topic exchange silently drops messages
  that have no bound queue. The alarm service must declare it with the same definition
  (`hastori_common.messaging`).
- **Restarts**: the ingestion MQTT session is persistent (`clean_start=False`, session expiry 1 h)
  under a fixed client id (`INGEST_CLIENT_ID`); Mosquitto keeps up to 200 000 messages for it
  (`max_queued_messages`, about 16 hours at 3.5 msg/s). A second replica needs its own stable id
  (`ingestion-2`, ...). Never use random ids with a shared subscription: the abandoned session
  stays in the group and the broker keeps handing it messages.
- **Shutdown**: SIGTERM stops reading, writes the queue out, acks and disconnects. If the
  database is down it gives up after `INGEST_SHUTDOWN_DEADLINE_S` (20 s) leaving the rest un-acked
  (redelivered later); compose allows 40 s.
- **Scaling**: shared subscription (`$share/ingestion/...`); a second replica splits the load.
- **Device credentials** are not stored: `HMAC-SHA256(MQTT_DEVICE_SECRET, device_id)`.
  Changing `MQTT_DEVICE_SECRET` changes every device password: run `make mqtt-auth`, restart
  mosquitto and the simulator.
- **Devices are never deleted**: a trigger refuses `DELETE` on `devices` (history references
  them); set `is_active = false`. Ingestion refreshes its device cache on a Postgres
  `LISTEN/NOTIFY` message, so changes apply immediately.
- **Migrations**: `0001` is frozen DDL (it does not import the ORM models). From day 2 on schema
  changes go in new migrations only; never edit an applied one. `alembic check` reports no drift
  between models and database.
- **Seed**: by default it never overwrites what people changed (user password hashes, alarm
  rules); `make seed-reset` does.

## Known limits

- Timestamps are kept to millisecond resolution; the device clock is trusted within a window of
  5 minutes in the past and 30 seconds in the future (otherwise the message is rejected).
- The continuous aggregate refreshes only the last 2 hours; data older than that (a bulk
  backfill) needs a manual `refresh_continuous_aggregate`.
- Mosquitto TLS uses a local demo CA (`make certs`); not for production. The CA key is kept in
  `infra/mosquitto/certs/` (git-ignored) so the server certificate can be renewed.
- The simulator keeps its state in memory; a restart starts every temperature from its normal
  value.

## What I would do differently in production

- Real PKI for broker and device certificates, secrets from a vault rather than `.env`.
- Mosquitto dynamic-security instead of generated password/ACL files, so adding a device does not
  need a broker restart.
- Redis, Caddy, Grafana and CI are deliberately deferred to days 2-4.
