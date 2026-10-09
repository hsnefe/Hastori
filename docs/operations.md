# Operations

Commands, ports and the API at a glance. Back to the [README](../README.md).

## Make targets

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
| `make backfill`  | `uv run python scripts/backfill.py --days 7` (`DAYS=3 make backfill`): synthetic history |
| `make simulate`  | `docker compose --profile sim up -d --build simulator` |
| `make fault`     | `uv run python scripts/fault.py izmir-komp-1 overheat 120` |
| `make smoke`     | `uv run python scripts/smoke.py` |
| `make smoke-quick` | `uv run python scripts/smoke.py --no-fault` |
| `make resilience`| `uv run python scripts/resilience.py` (about 30 minutes; `--quick` skips the 6 minute ingestion outage, `--alarm-only` runs just the alarm service drills) |
| `make e2e`       | `uv run python scripts/e2e.py` (about 25 minutes; `--skip-reactive`, `--skip-restart`) |
| `make web-install`, `make web-dev` | `cd web; npm ci` once, then `npm run dev` (dashboard on `http://127.0.0.1:3000`, `/api` is forwarded to the API on 8000, the WebSocket goes to 8000 directly) |
| `make web-check` | `cd web; npm run typecheck; npm run lint; npm test` |
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

## Endpoints (all bound to 127.0.0.1)

| Service | URL |
|---------|-----|
| **Dashboard** (Caddy: pages, REST and WebSocket on one origin) | `http://127.0.0.1:8080` |
| REST API, Swagger | `http://127.0.0.1:8000/api/v1`, `/api/v1/docs`, `/api/v1/openapi.json`; `/healthz`, `/readyz`, `/metrics` |
| **Grafana** (dashboard "Hastori pipeline") | `http://127.0.0.1:3001` (user `admin`, `GRAFANA_ADMIN_PASSWORD` in `.env`) |
| Prometheus (targets, alert rules) | `http://127.0.0.1:9090` (`/targets`, `/alerts`) |
| Alarm service health / readiness / metrics | `http://127.0.0.1:8003/healthz`, `/readyz`, `/metrics` |
| Ingestion health / readiness / metrics | `http://127.0.0.1:8001/healthz`, `/readyz`, `/metrics` |
| Simulator fault control | `http://127.0.0.1:8002/faults` (`Authorization: Bearer $SIM_CONTROL_TOKEN`) |
| RabbitMQ management | `http://127.0.0.1:15672` (credentials in `.env`) |
| TimescaleDB | `127.0.0.1:5432` |
| Redis | `127.0.0.1:6379` (password in `.env`) |
| MQTT (TLS only) | `127.0.0.1:8883` (CA: `infra/mosquitto/certs/ca.crt`) |

The ingestion and alarm endpoints, `/metrics` of the API and Prometheus have no authentication;
they only expose counters and readiness. Grafana and Prometheus see every site: they are never
routed through Caddy, reach them over SSH on a remote machine. Do not widen the `127.0.0.1` port bindings: put Caddy (8080) in front and, for the
internet, a tunnel (see [known limits](known-limits.md)).

## The API

| | |
|---|---|
| `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `GET /auth/me` | access token (15 min, in the body) + rotating refresh token (7 days, httpOnly cookie) |
| `POST /auth/password`, `GET`/`POST /auth/demo` | change your own password (ends your other sessions); is the passwordless demo sign-in on, and sign in as the demo viewer or site admin |
| `GET /sites`, `POST`/`PATCH /sites` | sites you can see; create and change (system admin) |
| `GET /sites/{id}/devices` | devices with their latest value per metric and an `online` flag |
| `GET /devices/{id}/measurements?metric=&from=&to=&interval=` | raw, 1 minute or 1 hour points (`auto` picks); at most 5000 points and 90 days |
| `GET /sites/{id}/consumption/daily?days=` | kWh per day of the site's time zone, with the share of minutes that have data |
| `GET /alarms`, `GET /alarms/{id}`, `POST /alarms/{id}/ack` | history, one alarm with its timeline, acknowledge |
| `GET`/`POST`/`PUT`/`DELETE /alarm-rules` | rule management (site admins for their sites); `DELETE` disables |
| `GET`/`POST /users`, `GET`/`PATCH /users/{id}` | user management (system admin); `PATCH` takes role, sites, password and `is_active` |
| `POST /ws-ticket`, `GET /ws?ticket=` (WebSocket) | live measurements and alarm events of one site at a time (see [design notes](design-notes.md)) |

A user sees only the sites assigned to them (a system admin sees all sites of the organisation).
An object of another site answers **404**, a role that may not do something **403**, a missing or
bad token **401**; a backing service that is down is a **503**. Every error has the same shape,
`{"error": {"code": "...", "message": "..."}}`.

## Upgrading from an earlier checkout

- Day 1 -> day 2: `make env` adds the new keys (`REDIS_PASSWORD`, `JWT_SECRET`, ...) to your
  `.env` without touching the existing ones. The alarm queue now has more arguments (dead-letter
  exchange, single active consumer) and RabbitMQ refuses to redeclare an existing queue with
  different ones, so run `make reset-alarm-queue` once, then `make up` again. Raw measurements are
  in the database; the alarm service replays the last minutes from there when it starts.
- The MQTT session id changed on day 1 (see below). Run
  `docker compose --profile sim down`, `docker volume rm hastori_mosquitto-data`, then `make up`;
  otherwise the old session `ingestion-1` stays in the shared-subscription group and the broker
  keeps handing it half of the messages until it expires.
