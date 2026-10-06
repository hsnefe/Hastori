.PHONY: env up down logs certs mqtt-auth migrate seed simulate fault smoke test lint

DEVICE ?= izmir-komp-1
KIND ?= overheat
DURATION ?= 40

env:
	@test -f .env || cp .env.example .env

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

simulate:
	docker compose --profile sim up -d --build simulator

fault:
	uv run python scripts/fault.py $(DEVICE) $(KIND) $(DURATION)

smoke:
	uv run python scripts/smoke.py

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages services scripts
