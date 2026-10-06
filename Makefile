.PHONY: env env-public up down logs certs mqtt-auth migrate seed seed-reset simulate fault smoke smoke-quick resilience test lint

DEVICE ?= izmir-komp-1
KIND ?= overheat
# Long enough for the 30 s alarm rule: the overheat ramp takes ~16 s to cross 80 C.
DURATION ?= 60

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

# Outage drills (about 15 minutes): restart / kill ingestion, a 6 minute ingestion outage, stop the
# broker / database / RabbitMQ, SIGTERM under failure. `--quick` skips the long one.
resilience:
	uv run python scripts/resilience.py $(ARGS)

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages services scripts
