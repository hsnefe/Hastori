.PHONY: env env-public up down logs certs mqtt-auth migrate seed seed-reset simulate fault smoke smoke-quick resilience reset-alarm-queue e2e test lint web-install web-dev web-check

DEVICE ?= izmir-komp-1
KIND ?= overheat
# Long enough for the 30 s alarm rule (the ramp takes ~16 s to cross 80 C) plus time to acknowledge.
DURATION ?= 120

env:
	uv run python scripts/gen_env.py

# A demo that is reachable from the internet: random demo-user passwords too (printed once)
env-public:
	uv run python scripts/gen_env.py --public

certs:
	uv run python scripts/gen_certs.py

mqtt-auth: env
	uv run python scripts/gen_mqtt_auth.py

up: env certs mqtt-auth
	docker compose up -d --build --wait

down:
	docker compose --profile sim down

logs:
	docker compose --profile sim logs -f --tail=100

migrate:
	docker compose run --rm migrate

seed:
	uv run python scripts/seed.py

# Demo reset: YAML wins again (user passwords, alarm rules)
seed-reset:
	uv run python scripts/seed.py --reset

simulate:
	docker compose --profile sim up -d --build simulator

fault:
	uv run python scripts/fault.py $(DEVICE) $(KIND) $(DURATION)

smoke:
	uv run python scripts/smoke.py

# Without the overheat check (it injects a real overheat fault into izmir-komp-1)
smoke-quick:
	uv run python scripts/smoke.py --no-fault

# Outage drills (about 30 minutes): restart / kill ingestion, a 6 minute ingestion outage, stop the
# broker / database / RabbitMQ, SIGTERM under failure, then the alarm service (RabbitMQ restart,
# kill -9, database outage, exiting by itself). `--quick` skips the long one.
resilience:
	uv run python scripts/resilience.py $(ARGS)

# After an upgrade that changed the alarm queue's arguments (RabbitMQ refuses to redeclare it)
reset-alarm-queue:
	uv run python scripts/reset_alarm_queue.py

# Day-2 checks against the running stack: authorization matrix, alarm lifecycle, restart, rule
# change, dead letters, reactive ratio (about 15 minutes; ARGS="--skip-reactive --skip-restart")
e2e:
	uv run python scripts/e2e.py $(ARGS)

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages services scripts tests conftest.py

# The dashboard with hot reload on http://127.0.0.1:3000; /api goes to the API on 8000 (Next rewrite)
# and the WebSocket straight to it (web/.env.development). The packaged one is on :8080.
web-install:
	cd web && npm ci

web-dev:
	cd web && npm run dev

# Lint, types and unit tests of the dashboard
web-check:
	cd web && npm run lint && npm run typecheck && npm test
