# Hastori

Industrial telemetry platform. A simulator publishes measurements from 7 devices every 2 seconds
over MQTT (TLS + per-device ACL) to an ingestion service, which writes them to TimescaleDB and
publishes them to RabbitMQ. An alarm service evaluates rules on that stream (hysteresis, duration,
reactive-energy ratio) and a REST API (JWT, three roles, strict per-site isolation) serves
devices, time series, daily consumption, alarms and rule management. The dashboard and the live
WebSocket come on days 3-4.

```
simulator --MQTT 5, QoS 1, TLS--> mosquitto --$share--> ingestion --+--> TimescaleDB (measurements + outbox)
 (7 devices, own credentials)                                       |
                                                                    +--> outbox relay --> RabbitMQ hastori.telemetry
                                                                                                 |
                                     alarm.telemetry (one active consumer, dead-letter queue) <-+
                                                 |
                                                 v
                          alarm service --alarms--> TimescaleDB <--reads (tenant-scoped)-- API (FastAPI, JWT)
                                 |                                                              |
                                 +----------- alarm.opened / .cleared --> Redis <---- sessions, login limit, .acknowledged
```

`seed/demo.yaml` is the single source of truth: the database seed, the Mosquitto password/ACL
files and the simulator device list are all generated from it.

## Hızlı başlangıç

Requirements: Docker + Compose, [uv](https://docs.astral.sh/uv/), Git. `make` is optional on
Windows (`winget install ezwinports.make`); PowerShell equivalents are below.

```bash
git clone <repo-url> hastori && cd hastori
make up          # .env with random secrets, TLS certs, MQTT users/ACL, then timescaledb + mosquitto + rabbitmq + redis + migrate + ingestion + alarm + api
make seed        # demo org, 2 sites, 7 devices, 5 users, 4 alarm rules (idempotent)
make simulate    # start the field simulator
make smoke       # automated day-1 checks
make e2e         # automated day-2 checks: authorization matrix, alarm lifecycle, restart, dead letters (about 15 minutes)
make fault DEVICE=izmir-komp-1 KIND=overheat   # trigger an overheat: a critical alarm opens after 30 s
```

Then open Swagger at <http://127.0.0.1:8000/api/v1/docs>, sign in with `POST /auth/login` as one
of the demo users, press *Authorize* and paste the `access_token`.

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

**Upgrading from an earlier checkout:**

- Day 1 -> day 2: `make env` adds the new keys (`REDIS_PASSWORD`, `JWT_SECRET`, ...) to your
  `.env` without touching the existing ones. The alarm queue now has more arguments (dead-letter
  exchange, single active consumer) and RabbitMQ refuses to redeclare an existing queue with
  different ones, so run `make reset-alarm-queue` once, then `make up` again. Raw measurements are
  in the database; the alarm service replays the last minutes from there when it starts.
- The MQTT session id changed on day 1 (see below). Run
  `docker compose --profile sim down`, `docker volume rm hastori_mosquitto-data`, then `make up`;
  otherwise the old session `ingestion-1` stays in the shared-subscription group and the broker
  keeps handing it half of the messages until it expires.

| make target      | PowerShell equivalent |
|------------------|-----------------------|
| `make env`       | `uv run python scripts/gen_env.py` (never overwrites an existing `.env`; only appends keys added to `.env.example`; refuses to create a new one while old data volumes exist; also writes `infra/redis/redis.pw`) |
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
| `make fault`     | `uv run python scripts/fault.py izmir-komp-1 overheat 120` |
| `make smoke`     | `uv run python scripts/smoke.py` |
| `make smoke-quick` | `uv run python scripts/smoke.py --no-fault` |
| `make resilience`| `uv run python scripts/resilience.py` (about 15 minutes; `--quick` skips the 6 minute outage) |
| `make e2e`       | `uv run python scripts/e2e.py` (about 15 minutes; `--skip-reactive`, `--skip-restart`) |
| `make reset-alarm-queue` | `uv run python scripts/reset_alarm_queue.py` (then `docker compose restart ingestion alarm`) |
| `make test`      | `uv run pytest` |
| `make lint`      | `uv run ruff check .; uv run ruff format --check .; uv run mypy packages services scripts tests conftest.py` |

Fault kinds: `overheat`, `spike`, `compensation_failure`, `offline`.
The default fault lasts 120 s: the overheat crosses 80 °C after 8-18 s, the demo alarm rule
(`seed/demo.yaml`) wants 30 s above it, and the alarm then stays open for over a minute so it
can be acknowledged; a unit test (`test_demo_scenario.py`) keeps the simulator and the rules in
step. `make smoke` injects a real overheat into `izmir-komp-1` and clears it as soon as 80 °C was
seen; use `make smoke-quick` whenever alarms are being tested. `compensation_failure` on
`izmir-pano` raises the reactive-ratio warning after about two minutes.

### Endpoints (all bound to 127.0.0.1)

| Service | URL |
|---------|-----|
| REST API, Swagger | `http://127.0.0.1:8000/api/v1`, `/api/v1/docs`, `/api/v1/openapi.json`; `/healthz`, `/readyz` |
| Alarm service health / readiness / metrics | `http://127.0.0.1:8003/healthz`, `/readyz`, `/metrics` |
| Ingestion health / readiness / metrics | `http://127.0.0.1:8001/healthz`, `/readyz`, `/metrics` |
| Simulator fault control | `http://127.0.0.1:8002/faults` (`Authorization: Bearer $SIM_CONTROL_TOKEN`) |
| RabbitMQ management | `http://127.0.0.1:15672` (credentials in `.env`) |
| TimescaleDB | `127.0.0.1:5432` |
| Redis | `127.0.0.1:6379` (password in `.env`) |
| MQTT (TLS only) | `127.0.0.1:8883` (CA: `infra/mosquitto/certs/ca.crt`) |

The ingestion and alarm endpoints have no authentication; they only expose counters and
readiness. Do not widen the `127.0.0.1` port bindings without putting the gateway (day 4) in
front.

### The API

| | |
|---|---|
| `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `GET /auth/me` | access token (15 min, in the body) + rotating refresh token (7 days, httpOnly cookie) |
| `GET /sites`, `POST`/`PATCH /sites` | sites you can see; create and change (system admin) |
| `GET /sites/{id}/devices` | devices with their latest value per metric and an `online` flag |
| `GET /devices/{id}/measurements?metric=&from=&to=&interval=` | raw, 1 minute or 1 hour points (`auto` picks); at most 5000 points and 90 days |
| `GET /sites/{id}/consumption/daily?days=` | kWh per day of the site's time zone, with the share of minutes that have data |
| `GET /alarms`, `GET /alarms/{id}`, `POST /alarms/{id}/ack` | history, one alarm with its timeline, acknowledge |
| `GET`/`POST`/`PUT`/`DELETE /alarm-rules` | rule management (site admins for their sites); `DELETE` disables |
| `GET`/`POST /users`, `GET`/`PATCH /users/{id}` | user management (system admin) |

A user sees only the sites assigned to them (a system admin sees all sites of the organisation).
An object of another site answers **404**, a role that may not do something **403**, a missing or
bad token **401**; a backing service that is down is a **503**. Every error has the same shape,
`{"error": {"code": "...", "message": "..."}}`.

## Design notes

### Ingestion (day 1)

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
  200 000 messages / 1 h, dead-lettering into `hastori.dlx` -> `alarm.telemetry.dlq`) next to the
  exchange, because a topic exchange silently drops messages that have no bound queue. Ingestion
  and the alarm service declare it through the same function (`hastori_common.messaging`).
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
- **Migrations**: `0001` is frozen DDL (it does not import the ORM models). Schema changes go in
  new migrations only; never edit an applied one (`0004` added alarm-rule checks and list-query
  indexes, `0005` rule kinds, the mandatory clear threshold and the site time zone). Each
  revision commits on its own, and `0002` is idempotent, so a failed run can simply be repeated.
  `alembic check` reports no drift between models and database.
- **TLS keys**: the CA private key lives in `infra/mosquitto/ca-private/` and is *not* mounted
  into the broker (only `ca.crt`, `server.crt`, `server.key` are). `make certs` is idempotent and
  renews the server certificate 30 days before it expires; run it before every `make up` (it is
  part of it) or from a scheduled task, then restart mosquitto.
- **Seed**: by default it never overwrites what people changed (users' e-mail, role and
  password hash, a site's name, city and time zone, a device's name and active flag, alarm rules);
  `make seed-reset` does.

### Alarm service (day 2)

- **State machine** (`engine.py`, pure: no I/O, no clock): per rule `normal -> pending -> active ->
  clearing -> normal`. The value must stay past the threshold for `duration_s` (a spike never
  alarms) and the alarm closes only after 10 s below the **mandatory** `clear_threshold`
  (hysteresis: a value hovering at the threshold neither opens nor closes it). Time is always the
  timestamp of the reading, so a device clock that runs behind, a backlog replayed at full speed
  and a restart all give the same answer. A silence of more than 10 s between two readings
  restarts a half-counted duration; an open alarm stays open while the device is silent (no data
  is not recovery). Acknowledging is not a state of the machine: an acknowledged alarm is still
  active.
- **Reactive ratio** (`kind: reactive_ratio`, energy analyzers only): inductive reactive energy /
  active energy over a sliding window (demo rule: 10 minutes, warn above 0.18, clear below 0.165).
  Capacitive power does not cancel inductive power; below 2 kWh of active energy in the window
  nothing is evaluated (a quiet night would otherwise swing the ratio). The limits are demo
  assumptions: real ones depend on the distribution company.
- **Delivery**: one active consumer reads `alarm.telemetry` in order and acks a message only
  after the transition it caused is committed. Writes are conditional (`INSERT ... ON CONFLICT`
  on the partial unique index "one open alarm per rule", `UPDATE ... WHERE state <> 'cleared'`),
  so redelivery, a restart, or two overlapping instances can neither open nor close an alarm
  twice. A database outage is retried, never dropped; a malformed message is rejected into the
  dead-letter queue (`alarm.telemetry.dlq`, bounded) instead of looping.
- **Restart**: the service keeps no state that is not a function of stored data. At start it reads
  the rules and the open alarms, then replays the last 15 minutes (or the longest rule duration or reactive window plus a
  minute; `duration_s` is capped at 600 s) of `measurements` through the same state machine. A transition a previous run already wrote is
  recognised by a per-rule watermark and not written again; an alarm that is open in the database
  is adopted and not closed by data older than its opening; an alarm whose rule was disabled while
  the service was down is closed.
- **Rule changes** reach the running service through `LISTEN alarm_rules_changed` (a trigger on
  `alarm_rules`): a new threshold restarts a pending count, a disabled rule closes its open alarm,
  within a second or two.
- **Events**: `alarm.opened` and `alarm.cleared` (and `alarm.acknowledged`, from the API) go to
  Redis channel `hastori:site:{site_id}` after the commit. Pub/sub guarantees nothing: a screen
  that reconnects reads the current state over REST and treats events as hints.
- `/metrics`: `alarm_messages_total`, `alarm_rejected_total{reason}`, `alarm_transitions_total{to}`,
  `alarms_open`, `alarm_eval_lag_seconds`, `alarm_event_publish_failures_total`.

### API (day 2)

- **Tenant isolation is structural.** Every query function takes the user's `SiteScope` as its first
  argument and starts from a base query that already carries the site filter; routers contain no
  SQL (a test fails if one does, another if a query function lacks the scope parameter, another if
  an endpoint is missing from the authorization matrix). The role, the organisation and the site
  list are read from the database on every request, never from the token: demoting a user or
  removing a site applies to the token they already hold. A site that is not yours does not exist
  as far as you can tell (404), not even by its error.
- **Authorization matrix** (`scripts/api_matrix.py`): every endpoint x every demo user -> the status
  code it must give (about 190 cells). The in-process tests run it, `make e2e` runs it again on the
  live stack, and a meta-test proves that removing a filter makes it fail.
- **Tokens.** Access token: HS256 JWT, 15 minutes, issuer, audience, expiry and algorithm checked
  (`alg=none` and other algorithms are refused). Refresh token: opaque 256 bits, only its SHA-256
  is stored in Redis, one family per login, rotated on every use by one atomic Lua script. A used
  token still works for 20 s (two tabs refreshing at once get a token each); a use after that is a
  stolen copy and revokes the whole family. The cookie is httpOnly, `SameSite=Strict`, scoped to
  `/api/v1/auth` (`COOKIE_SECURE=true` behind HTTPS).
- **Sign-in.** Argon2 verification runs in a thread, at most two at a time (each needs 64 MiB), and also for
  unknown users (same answer, same cost); 5 failed attempts per e-mail address and client address block further attempts for 5
  minutes (429 with `Retry-After`).
- **Acknowledging** is one conditional `UPDATE ... WHERE state = 'active'`: two people at once, or
  the alarm service closing the alarm meanwhile, give one winner and a 409.
- **Daily consumption** sums the main panel (energy analyzer) only: it already measures everything
  behind it. kWh is the integral of power from the 1-minute aggregate; minutes without data add
  nothing and lower `coverage`, so a total with a hole is reported as one. The day boundaries come
  from the site's time zone, computed in Python and handed to SQL as UTC ranges (a day is not
  always 24 hours).
- **Audit log**: one function writes it, inside the transaction of the change; a password change
  records that it happened, never the value.
- **Rules** are validated in one place (hysteresis on the right side, ratio rules only on energy
  analyzers with a window, finite numbers) and by the database (CHECK constraints, one enabled
  rule per device, metric and kind). A rule is never deleted, alarms refer to it: `DELETE`
  disables it.

## Tests

`make test` needs no Docker: it starts a real PostgreSQL (the `pgserver` package) and a Redis that
runs Lua (`fakeredis`), builds the schema with the real migrations and runs the services against
them: about 230 tests, several of them on the properties that matter most (the state machine in
virtual time including a restart after every possible sample; the authorization matrix; refresh
token rotation under parallel requests). What this cannot show is TimescaleDB itself (migration
`0002` is replaced by a plain table and view with the same columns), RabbitMQ, the MQTT broker and
the containers: `make smoke`, `make resilience` and `make e2e` check those on the running stack.

## Known limits

- The alarm service evaluates in one process (one active queue consumer): at demo load (about 3.5
  messages per second) that is far from a limit; sharding by device is the way beyond it.
- There is no "device offline" alarm: a silent device keeps its open alarm and shows
  `online: false` in the device list. A pending count that would span a silence restarts.
- A password change does not end the user's refresh sessions (the access token is checked against
  the database on every request, the refresh token only against Redis).
- The login limiter counts per e-mail address and client address; an attacker who rotates
  addresses is only stopped by the gateway's limit (day 4).
- The refresh rotation script touches keys it builds itself: it needs a single Redis, not a
  Redis Cluster.
- `GET /sites/{id}/consumption/daily` uses the 1-minute averages, so reactive energy of a minute
  that is partly capacitive is slightly underestimated. On 7 days of data
  for two panels it runs in about 30 ms (`EXPLAIN (ANALYZE)`, chunks are excluded).
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
- Postgres, RabbitMQ, Mosquitto and Redis data volumes are plain Docker volumes: back them up
  before `docker compose down -v`. Redis holds sessions only; losing it signs everybody out.
- No `LICENSE` file yet: the licence is the owner's choice.
- `docs/design-risks.md` lists the risks of what is *not built yet* (WebSocket, dashboard,
  gateway, CI, public hosting) and what is still open of the rest.
- Mosquitto TLS uses a local demo CA (`make certs`); not for production. The CA key is kept in
  `infra/mosquitto/certs/` (git-ignored) so the server certificate can be renewed.
- The simulator keeps its state in memory; a restart starts every temperature from its normal
  value.

## What I would do differently in production

- Real PKI for broker and device certificates, secrets from a vault rather than `.env`.
- Mosquitto dynamic-security instead of generated password/ACL files, so adding a device does not
  need a broker restart.
- PostgreSQL row-level security as a second line behind the `SiteScope` filter; the alarm
  service sharded by device (consistent hashing) instead of one active consumer; the alarm events
  through an outbox like the telemetry, so a Redis outage cannot hide an alarm from live screens.
- Refresh sessions tied to a per-user token version, so a password change or a deactivation ends
  them at once; the access token's lifetime is the only window left.
- Redis, Caddy, Grafana and CI are deliberately deferred to days 3-4.
