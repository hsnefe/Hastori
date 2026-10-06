"""Tests for the helper scripts (they are not packages, so they are loaded by path)."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

from hastori_common.messaging import ALARM_QUEUE_ARGS, DLQ_ARGS, DLX

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_env_has_no_demo_secrets_and_consistent_urls() -> None:
    gen_env = load("gen_env")
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    text, values = gen_env.build_new(template, public=False)
    env = gen_env.parse(text)

    assert len(values["JWT_SECRET"]) == 64
    assert env["REDIS_URL"] == f"redis://:{values['REDIS_PASSWORD']}@127.0.0.1:6379/0"
    assert env["DATABASE_URL"].endswith(f"{values['POSTGRES_PASSWORD']}@127.0.0.1:5432/hastori")
    for demo in ("redis_demo", "hastori_demo", "change-me-demo", "demo-control-token"):
        assert demo not in text, demo


def test_every_secret_key_exists_in_the_template() -> None:
    gen_env = load("gen_env")
    template = gen_env.parse((ROOT / ".env.example").read_text(encoding="utf-8"))
    assert set(gen_env.SECRET_KEYS) <= set(template)


def test_alarm_queue_dead_letters_into_a_bounded_queue() -> None:
    assert ALARM_QUEUE_ARGS["x-dead-letter-exchange"] == DLX
    assert ALARM_QUEUE_ARGS["x-single-active-consumer"] is True
    assert DLQ_ARGS["x-max-length"] > 0 and "x-dead-letter-exchange" not in DLQ_ARGS


def test_compose_services_do_not_publish_ports_beyond_loopback() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    ports = re.findall(r'^\s+- "([^"]+:\d+:\d+)"', compose, flags=re.M)
    assert ports, "no port mappings found"
    assert all(p.startswith("127.0.0.1:") for p in ports), ports
