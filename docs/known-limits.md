# Known limits

Back to the [README](../README.md).

- **One main meter per site.** The daily kWh sums the site's main panel (the energy analyzer). A
  site with two panels in series would be counted twice; the demo sites have one. There is no
  column to say which panel counts yet (it would be a new migration). Only active panels are
  summed and counted for `coverage`, so deactivating a panel also hides its past days.
- **A device clock set back (B3).** A sample 60 s or more behind the device's newest one restarts
  that device's sequence (`alarm_clock_jumps_total`), so the alarm keeps working. Right after the
  alarm service starts, a jump deeper than the start-up replay window (15 minutes) looks like a
  queued backlog until a live reading arrives, and is ignored until then. Smaller slips are still
  dropped as out of order.
- The dashboard has no user or site management screens and no per-user language: the interface is
  Turkish, code and docs English. The rules page lists and edits rules but creates none (a new
  rule, `no_data` included, goes through the API).

- The alarm service evaluates in one process (one active queue consumer): at demo load (about 3.5
  messages per second) that is far from a limit; sharding by device is the way beyond it.
- A silent device shows `online: false` in the device list and an offline badge on its card; an
  alarm for it exists only where a `no_data` rule is set (the demo has one, on `izmir-komp-1`).
  A threshold rule's open alarm stays open while its device is silent, and a pending count that
  would span a silence restarts.
- A refresh session ends at the latest 30 days after the sign-in (`REFRESH_MAX_LIFE_S`), however
  often it is renewed. A password change or deactivation ends sessions through the token version;
  there is no screen for it yet (the API has it), and users cannot be deleted, only deactivated.
- The login limiter counts failures per e-mail address and client address (5 in 5 minutes) and
  per client address alone (30); an attacker with many addresses is only slowed by that. Caddy
  adds a limit per client address in front (10 a minute). At most 16 password hashes wait at a time, the rest of the sign-ins get a quick
  503 instead of queueing behind them.
- WebSockets: 5 per user, 500 in all, 30 tickets a minute per user (`WS_MAX_PER_USER`,
  `WS_MAX_TOTAL`, `WS_TICKETS_PER_MINUTE`). uvicorn reads at most 8 KiB per client message.
- **Putting it on the internet:** `make env-public` (random demo passwords, Swagger off), a
  tunnel in front of Caddy and nothing else published. A direct visitor of `127.0.0.1:8080` is a
  "private" peer to Caddy and could claim any client address; behind a tunnel that is not
  reachable. Switch `COOKIE_SECURE=true` once there is HTTPS.
- A second alarm replica waits as a standby (single active consumer). Before its first message
  after two minutes without one it rebuilds its engine from the database, so it takes over with
  the alarms the other replica opened. After a failed write the active one rebuilds too.
- A rule whose reactive window holds under 2 kWh has no ratio: an open reactive alarm stays open
  until the window fills again (no data is not recovery). The database caps `duration_s` at 600 s.
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
- `docs/design-risks.md` lists the risks of what is *not built yet* (gateway hardening, CI,
  public hosting) and what is still open of the rest.
- Mosquitto TLS uses a local demo CA (`make certs`); not for production. The CA key is kept in
  `infra/mosquitto/ca-private/` (git-ignored, not mounted into any container) so the server
  certificate can be renewed.
- The simulator keeps its state in memory; a restart starts every temperature from its normal
  value.

## What I would do differently in production

- Real PKI for broker and device certificates, secrets from a vault rather than `.env`.
- Mosquitto dynamic-security instead of generated password/ACL files, so adding a device does not
  need a broker restart.
- PostgreSQL row-level security as a second line behind the `SiteScope` filter; the alarm
  service sharded by device (consistent hashing) instead of one active consumer; the alarm events
  through an outbox like the telemetry, so a Redis outage cannot hide an alarm from live screens.
- Alertmanager with a real notification channel (e-mail, chat) for the Prometheus alerts.
- Alarm name, severity and metric are read from the rule when an alarm is shown, so editing a
  rule renames its past alarms (the thresholds an alarm opened with are stored). Snapshot
  columns on `alarms` would fix it.
