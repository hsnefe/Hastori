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
and `antalya.admin@demo.hastori.local` (site admins), `izmir.izleyici@demo.hastori.local` and
`antalya.izleyici@demo.hastori.local` (viewers; two sites make the tenant isolation visible);
passwords are the `SEED_*` values in `.env` (`admin_demo_pw`, `siteadmin_demo_pw`,
`viewer_demo_pw` by default). All other secrets in `.env` are generated randomly by `make env`.
**Those three are public.** Never expose the stack beyond `127.0.0.1` (Cloudflare Tunnel, a
public server) without `make env-public` first.

Services refuse to start with a published demo value in a secret they use
(`ALLOW_DEMO_SECRETS=1` overrides this for throwaway runs). Each container receives only the
variables it needs, runs as an unprivileged user with a read-only root filesystem, no
capabilities and a memory limit.

**Upgrading from an earlier checkout:** the MQTT session id changed (see below). Run
`docker compose --profile sim down`, `docker volume rm hastori_mosquitto-data`, then `make up`;
otherwise the old session `ingestion-1` stays in the shared-subscription group and the broker
keeps handing it half of the messages until it expires.

| make target      | PowerShell equivalent |
|------------------|-----------------------|
| `make env`       | `uv run python scripts/gen_env.py` (never overwrites an existing `.env`; only appends keys added to `.env.example`; refuses to create a new one while old data volumes exist) |
| `make env-public`| `uv run python scripts/gen_env.py --public` (also random demo-user passwords, printed once: use it before exposing the demo on the internet) |
| `make certs`     | `uv run python scripts/gen_certs.py` (renews the server certificate when < 30 days are left) |
| `make mqtt-auth` | `uv run python scripts/gen_mqtt_auth.py` |
| `make up`        | env + certs + mqtt-auth above, then `docker compose up -d --build --wait` |
| `make down`      | `docker compose --profile sim down` |
| `make logs`      | `docker compose --profile sim logs -f --tail=100` |
| `make migrate`   | `docker compose run --rm migrate` |
| `make seed`      | `uv run python scripts/seed.py` |
| `make seed-reset`| `uv run python scripts/seed.py --reset` (YAML wins: re-hash passwords, restore rules) |
| `make simulate`  | `docker compose --profile sim up -d --build simulator` |
| `make fault`     | `uv run python scripts/fault.py izmir-komp-1 overheat 60` |
| `make smoke`     | `uv run python scripts/smoke.py` |
| `make smoke-quick` | `uv run python scripts/smoke.py --no-fault` |
| `make resilience`| `uv run python scripts/resilience.py` (about 15 minutes; `--quick` skips the 6 minute outage) |
| `make test`      | `uv run pytest` |
| `make lint`      | `uv run ruff check .; uv run ruff format --check .; uv run mypy packages services scripts` |

Fault kinds: `overheat`, `spike`, `compensation_failure`, `offline`.
The default fault lasts 60 s: the overheat crosses 80 °C after ~16 s and the demo alarm rule
(`seed/demo.yaml`) wants 30 s above it; a unit test keeps the two in step. `make smoke` injects a
real overheat into `izmir-komp-1` and clears it as soon as 80 °C was seen; use `make smoke-quick`
whenever alarms are being tested.

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
- **Poison messages**: only errors that are a property of the data (SQLSTATE class 22 data
  exception, class 23 integrity violation, e.g. a foreign key) switch a batch to row-by-row
  commits so that only the offending message is dropped (`reason="db_rejected"`). Every other
  database error (connection, restart, failover, a missing table after a bad migration, a wrong
  password) is retried with backoff and nothing is acked meanwhile: an outage must never turn
  into silent data loss. The payload parser raises `Rejected` for any input, however hostile
  (huge numbers, deep nesting, oversized, binary), so one bad message cannot kill the connection
  and be redelivered forever.
- **Liveness**: the loops (writer, relay, subscriber, device cache, HTTP) are watched. If one
  dies the process logs it, exits non-zero and the restart policy takes over; `/healthz` returns
  503 meanwhile. `/readyz` reports MQTT, database and RabbitMQ (the latter really reflects a lost
  broker connection).
- **Outbox relay**: publishes with a timeout and holds no database transaction while talking to
  RabbitMQ, so a broker under a memory/disk alarm cannot pin a connection or block vacuum.
- **Durable queue**: ingestion declares `alarm.telemetry` (bound to `telemetry.#`, bounded to
  200 000 messages / 1 h) next to the exchange, because a topic exchange silently drops messages
  that have no bound queue. The alarm service must declare it with the same definition
  (`hastori_common.messaging`).
- **Restarts**: the ingestion MQTT session is persistent (`clean_start=False`, session expiry
  1 h). The broker keeps up to 200 000 messages for it, but only while the session lives, so an
  outage is survivable for **1 hour** (not "16 hours": that is how long the queue *could* grow).
  A replayed message may be up to that old; the parser accepts timestamps up to 65 minutes in the
  past (`MAX_AGE_S`), a test pins the two numbers together, and `make resilience` includes a
  6 minute outage. Mosquitto delivers up to 500 messages in flight (`max_inflight_messages`), so a
  backlog replays at hundreds of messages per second, not 20.
- **MQTT identity**: `use_username_as_clientid` makes the MQTT user the client id. Without it any
  authenticated device could connect as `ingestion` and take over its session, discarding the
  queued messages (upstream issue eclipse-mosquitto/mosquitto#3553, open). A second ingestion
  replica therefore needs its own MQTT user and ACL entry, and its own `INGEST_HTTP_PORT`; never
  use random ids with a shared subscription: the abandoned session stays in the group and the
  broker keeps handing it messages. `message_size_limit` is 4096 bytes, `max_connections` 200.
- **Shutdown**: SIGTERM stops reading, writes the queue out, acks and disconnects. If the
  database is down it gives up after `INGEST_SHUTDOWN_DEADLINE_S` (20 s) leaving the rest un-acked
  (redelivered later); compose allows 40 s.
- **Scaling**: shared subscription (`$share/ingestion/...`); a second replica splits the load
  (see "MQTT identity" for what it needs; the compose file runs one).
- **Device credentials** are not stored: `HMAC-SHA256(MQTT_DEVICE_SECRET, device_id)`.
  Changing `MQTT_DEVICE_SECRET` changes every device password: run `make mqtt-auth`, restart
  mosquitto and the simulator.
- **Devices are never deleted**: a trigger refuses `DELETE` on `devices` (history references
  them); set `is_active = false`. Ingestion refreshes its device cache on a Postgres
  `LISTEN/NOTIFY` message, so changes apply immediately.
- **Migrations**: `0001` is frozen DDL (it does not import the ORM models). From day 2 on schema
  changes go in new migrations only; never edit an applied one (`0004` added alarm-rule checks
  and list-query indexes). Each revision commits on its own, and `0002` is idempotent, so a
  failed run can simply be repeated. `alembic check` reports no drift between models and database.
- **TLS keys**: the CA private key lives in `infra/mosquitto/ca-private/` and is *not* mounted
  into the broker (only `ca.crt`, `server.crt`, `server.key` are). `make certs` is idempotent and
  renews the server certificate 30 days before it expires; run it before every `make up` (it is
  part of it) or from a scheduled task, then restart mosquitto.
- **Seed**: by default it never overwrites what people changed (user password hashes, alarm
  rules); `make seed-reset` does.

## Known limits

- Timestamps are kept to millisecond resolution; the device clock is trusted within a window of
  65 minutes in the past (a replayed backlog) and 30 seconds in the future (otherwise the
  message is rejected). The device clock decides the timestamp: a drifting clock shifts a
  reading, it is not corrected.
- The continuous aggregate refreshes only the last 2 hours; data older than that (a bulk
  backfill) needs a manual `refresh_continuous_aggregate` **with a window that stays inside the
  7 day raw retention**. Never refresh with `NULL, NULL`: it would rebuild the aggregate from the
  raw data that is left and delete the older minutes already materialised.
- A device that floods valid, fresh messages is only bounded by the 10 000-message queue and the
  1 s batch writer; there is no per-device rate limit (a limit by arrival time would also throttle
  legitimate backlog replays). Rejected messages are counted exactly but logged at most once per
  10 s per reason.
- `timescale/timescaledb:2.17.2-pg17` is pinned on purpose: moving an existing volume to a newer
  image needs `ALTER EXTENSION timescaledb UPDATE` and a look at the release notes.
- The mosquitto healthcheck passes its password on the command line inside the container (not
  visible from the host); there is no `-o` options file for it in the image.
- Postgres, RabbitMQ and Mosquitto data volumes are plain Docker volumes: back them up before
  `docker compose down -v`.
- No `LICENSE` file yet: the licence is the owner's choice.
- `docs/design-risks.md` lists the risks of what is *not built yet* (alarm service, API,
  WebSocket, dashboard, gateway, CI, public hosting).
- Mosquitto TLS uses a local demo CA (`make certs`); not for production. The CA key is kept in
  `infra/mosquitto/certs/` (git-ignored) so the server certificate can be renewed.
- The simulator keeps its state in memory; a restart starts every temperature from its normal
  value.

## What I would do differently in production

- Real PKI for broker and device certificates, secrets from a vault rather than `.env`.
- Mosquitto dynamic-security instead of generated password/ACL files, so adding a device does not
  need a broker restart.
- Redis, Caddy, Grafana and CI are deliberately deferred to days 2-4.
