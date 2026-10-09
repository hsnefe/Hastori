# Design notes

How each part works and why. Back to the [README](../README.md); decisions are in [adr/](adr/), open risks in [design-risks.md](design-risks.md).

## Ingestion (day 1)

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

## Observability (day 4)

- Prometheus scrapes ingestion, the alarm service, the API and RabbitMQ (its `rabbitmq_prometheus`
  plugin; per-queue depths from `/metrics/detailed`) every 15 s and keeps 7 days (at most 1 GB).
- The API exports `api_requests_total{method,route,status}`, `api_request_duration_seconds` and
  `api_ws_connections`. `route` is the template (`/api/v1/sites/{site_id}/devices`), never an id;
  probes and the scrape itself are not counted.
- Alert rules (`infra/prometheus/alerts.yml`): a service down, the whole fleet silent (the case
  `no_data` cannot see, risk G16), a backlog on `alarm.telemetry`, a growing dead-letter queue or
  rejected messages, evaluation lag over 10 s, an outbox backlog, ingestion rejecting, MQTT
  reconnect churn, API 5xx. There is no notification channel: firing alerts show in Prometheus and
  on the Grafana dashboard.
- Grafana is provisioned from `infra/grafana` (data source and the "Hastori pipeline" dashboard:
  flow, lag, open alarms, queues, API traffic and latency, WebSockets, firing alerts); anonymous
  access and sign-up are off.
- Request ids: Caddy makes one per request (never taken from the client), sends it to the API as
  `X-Request-ID`, writes it into its access log as `request_id` and returns it to the browser. The
  API logs every line of that request with the same `request_id` and echoes it, also on a 500.

## Demo data and demo sign-in (day 4)

- **Synthetic history.** `make backfill` writes 1 to 7 days (default 7) of one-minute readings
  from the simulator's own signal model (`hastori_simulator.signals`: the factory's shift, the
  hotel's evening peak), refreshes the one-minute aggregate for exactly that window and stops where
  real data starts. It never overwrites a row, injects no faults and opens no alarms. **The numbers
  on the energy card before the first real day are synthetic**, not measured. Raw data is kept 7
  days, so more is refused; the refresh always has both bounds (`NULL, NULL` would rebuild the
  aggregate from what raw data is left and delete older minutes, risk D1).
- **Demo sign-in.** With `DEMO_LOGIN=true`, `POST /auth/demo {"role": "viewer" | "site_admin"}` signs in
  as the one fixed Izmir viewer or Izmir site admin (`DEMO_VIEWER_EMAIL`, `DEMO_SITE_ADMIN_EMAIL`)
  and answers like `/auth/login`. The system admin cannot be asked for, the account must have
  the requested role (a wrong e-mail setting does not become a back door) and be active; with the
  setting off the endpoint is a 404. The gateway counts it with sign-in. A site admin can change
  rules: on a public demo run `make seed-reset` nightly (risk G17).
- **Sessions end when they should.** `users.token_version` goes up on a password change (your own or
  an admin's) and on deactivation (`PATCH /users/{id} {"is_active": false}`; not yourself, not the
  last active system admin). The access token carries the version and every request compares it,
  the refresh session keeps the version it was signed in under: both die at once, a deactivated
  user cannot sign in and gets the same 401 as a wrong password. `POST /auth/password` needs the
  current password and keeps the device it was called from signed in.
- **Who acknowledged.** `alarms.acked_by_label` stores the name at acknowledgement (the e-mail
  address before the `@`: users have no other name). The API and the screen show that, never the
  address, and it stays when the account is renamed. Migration `0009` fills it for old alarms.

## Dashboard (day 3)

- **One origin.** In the packaged stack Caddy listens on `127.0.0.1:8080` and sends `/api/*` (REST
  and the WebSocket) to the API and everything else to the Next.js server, so the refresh cookie
  (`SameSite=Strict`, `Path=/api/v1/auth`) is first-party and the socket needs no CORS. With
  `make web-dev` the same job is done by a Next.js rewrite. Caddy's access log drops the
  WebSocket ticket from the query string (errors too). Caddy hands the API the real client address
  (`CF-Connecting-IP` / `X-Forwarded-For` from private proxy ranges; the API believes it only
  from `TRUSTED_PROXIES`).
- **What the gateway does** (`infra/caddy`, an image built with `xcaddy` and `mholt/caddy-ratelimit`):
  10 requests a minute per client address for sign-in and token refresh, 300 for the rest of
  `/api` (429 with `Retry-After`); a request body over 1 MB is refused (413); a `Host` that is not
  in `ALLOWED_HOSTS` gets 421; `/api` answers carry `Cache-Control: no-store`; `Strict-Transport-Security`
  is sent when the tunnel says the request came over HTTPS (`X-Forwarded-Proto`), and then the CSP lets
  the page open `wss:` only; open WebSockets get a minute to finish on a reload. TLS itself ends
  at the tunnel in front. The base images are pinned by digest.
- **Why Next.js, and how little of it is used.** It is the stack of the brief. Used: the App Router for layouts
  and the dynamic `/sites/[siteId]` routes, a server-rendered page skeleton, the standalone
  output for a small image, and `proxy.ts` for a per-request CSP nonce. Not used: Server Actions
  or server-side data fetching. The refresh cookie only travels to `/api/v1/auth`, so the Next.js
  server cannot know who is signed in; all data is fetched by the browser with the user's access
  token. Anything that renders data, formats a time or draws a chart is a client component.
- **Session.** The access token lives in memory only (not in `localStorage`, not in a cookie
  JavaScript can read). On every page load the httpOnly refresh cookie is exchanged for a new one;
  parallel 401s share one refresh and each request is retried once; a 503 waits for `Retry-After`.
  After sign-out the refresh answers 401.
- **WebSocket.** A browser cannot set an `Authorization` header on a socket, and a token in the URL
  ends up in logs and history, so the page first asks `POST /ws-ticket` (bearer) for a ticket:
  32 random bytes, kept in Redis as a SHA-256 for 30 s, consumed with `GETDEL` when the socket
  opens (a second use closes with 4401). The user's `SiteScope` is built from the database when
  the socket opens and the client subscribes to **one site at a time**; a site outside the scope
  closes the socket with 4403, the socket's version of the REST 404. A browser `Origin` that is not
  in `WS_ALLOWED_ORIGINS` is refused with 403. A connection lives at most 15 minutes (4408) and
  the page reconnects with a new ticket, so a changed role or site list takes effect within 15
  minutes at the latest.
- **Events are hints, REST is the truth.** One Redis `PSUBSCRIBE hastori:site:*` per API process
  feeds a hub that routes by site. Pub/sub can lose a message, so the server sends `resync` right
  after a subscription and to everyone when its Redis link comes back, and the page then fetches
  the current state over REST (the alarm list also refreshes every 60 s). Every connection has its
  own 256-message queue and writer task: a slow client is closed with 1013 and nobody else waits.
  The server sends `hb` every 25 s (Cloudflare drops a silent socket at about 100 s); the page
  reconnects after 60 s without any message, with a 1 s to 30 s randomised backoff.
- **Measurements.** After each batch commits, ingestion publishes one `MeasurementEvent` per
  sample to the site's channel from a separate task with its own bounded queue: a Redis outage
  costs live events (`ingest_event_publish_failures_total`), never a write, an ack or the outbox,
  and it does not stop the chart when the alarm service is down.
- **The chart.** One REST read for the last 15 minutes, then the socket appends; the window slides,
  the chart redraws at most once a second without animation, and the axis reaches the latest point
  when a device clock is ahead. "Online" is "a reading reached this screen in the last 30 s".
- **Daily kWh** shows today's value with its coverage; a day with less than 95 % of its minutes
  is drawn dashed and labelled as partial, never as a complete day. The sample data only has the
  days the stack has been running.
- **Alarms** open and acknowledge without a refresh; the acknowledge button exists for site and
  system admins only (the API enforces it again), a 409 says someone else got there first. An
  alarm keeps the thresholds it opened with (`alarms.threshold`, `clear_threshold`), so editing a
  rule later does not change what the history says.

## Alarm service (day 2)

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
- **No data** (`kind: no_data`): a device that stops reporting a metric for `duration_s` seconds
  (10 to 600; demo rule: 60 s of `temperature_c` on `izmir-komp-1`) opens a warning, and it closes
  after the metric has been arriving again for 10 s. It has no thresholds (stored as 0 and `>`,
  left out of the API body). The other kinds fire on a message; this one fires on the absence of
  one, so the service also looks at the clock every 5 s (`Engine.tick`) and measures silence in the
  time a reading *arrived*, not in device time: a device whose clock is an hour off is not silent,
  and a backlog the broker hands over after an outage is not old. Silence is only the device's
  fault while the rest of the pipeline works: the alarm opens only if a reading of some device
  arrived at or after the moment the limit was crossed, and when readings return after 20 s
  without any, every silence clock starts again. A broker, ingestion or RabbitMQ outage therefore
  blames nobody (`make resilience` stops ingestion and RabbitMQ for 90 s and expects no alarm). A
  consequence: if the whole fleet falls silent at once (every site loses power), that cannot be told
  from a pipeline outage and raises no `no_data` alarm. The alarm's peak value is the length of the
  silence in seconds.
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

## API (day 2)

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
