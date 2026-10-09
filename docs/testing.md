# Tests and CI

Back to the [README](../README.md).

`make test` needs no Docker: it starts a real PostgreSQL (the `pgserver` package) and a Redis that
runs Lua (`fakeredis`), builds the schema with the real migrations and runs the services against
them: about 360 Python tests (plus 74 Vitest tests for the dashboard: `make web-check`), several of them on the properties that matter most (the state machine in
virtual time including a restart after every possible sample; the authorization matrix; refresh
token rotation under parallel requests). What this cannot show is TimescaleDB itself (migration
`0002` is replaced by a plain table and view with the same columns), RabbitMQ, the MQTT broker and
the containers: `make smoke`, `make resilience` and `make e2e` check those on the running stack.

`make test-integration` (needs Docker, about 30 s) closes most of that gap without the stack:
Testcontainers starts the compose images of TimescaleDB and RabbitMQ, runs every migration (`0002`
included: hypertable, continuous aggregate, retention and refresh policies), and drives the
ingestion writer, the outbox relay with the real publisher and the alarm consumer (an alarm opened
from the real queue, a bad message dead-lettered) against them.

CI (`.github/workflows/ci.yml`, every push and pull request) runs the same: `ruff` and the format
check, `mypy`, the migrations with looser rules (`make lint-migrations`), `pytest` on Linux,
`make test-integration` as a job of its own, the
dashboard's lint, types and tests, `npm audit --omit=dev`, `docker compose config`, `promtool`
on the Prometheus rules, `caddy validate`, and a build of every image (nothing is pushed). The
actions are pinned to commit SHAs.
