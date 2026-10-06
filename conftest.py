"""Shared fixtures: a real PostgreSQL and a Redis that runs Lua, without Docker.

`pgserver` ships PostgreSQL binaries; the schema is built by the real migrations. Migration 0002
(hypertable, continuous aggregate, retention) needs TimescaleDB, which is not available here, so
a plain table and a plain view with the same columns stand in for it and the revision is stamped:
everything else (0001, 0003, 0004, 0005) really runs. What this cannot prove (TimescaleDB itself,
RabbitMQ, the MQTT broker) is covered by `make smoke`, `make resilience` and `make e2e` against the
running stack.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent

# What migration 0002 builds, minus TimescaleDB: same columns, same unique index. The view groups
# by minute like the continuous aggregate (date_trunc instead of time_bucket).
PLAIN_MEASUREMENTS_SQL = """
CREATE TABLE measurements (
    time timestamptz NOT NULL,
    device_id uuid NOT NULL REFERENCES devices(id),
    metric text NOT NULL
        CHECK (metric IN ('active_power_kw','reactive_power_kvar','current_a','temperature_c')),
    value double precision NOT NULL
);
CREATE UNIQUE INDEX uq_measurements ON measurements (device_id, metric, time DESC);
CREATE VIEW measurements_1m AS
SELECT date_trunc('minute', time) AS bucket, device_id, metric,
       avg(value) AS avg_value, min(value) AS min_value, max(value) AS max_value
FROM measurements GROUP BY 1, 2, 3;
"""


def with_database(uri: str, name: str) -> str:
    parts = urlsplit(uri)
    return urlunsplit(parts._replace(path=f"/{name}"))


def alembic_config(dsn: str) -> Config:
    """An Alembic config that targets `dsn` (env.py reads DATABASE_URL through the settings)."""
    from hastori_common.settings import get_settings

    os.environ["DATABASE_URL"] = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    get_settings.cache_clear()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


async def _admin(uri: str, *statements: str) -> None:
    conn = await asyncpg.connect(with_database(uri, "postgres"))
    try:
        for sql in statements:
            await conn.execute(sql)
    finally:
        await conn.close()


async def _run_sql(dsn: str, sql: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def pg_uri() -> Iterator[str]:
    pgserver = pytest.importorskip("pgserver")
    data = Path(tempfile.mkdtemp(prefix="hastori_pg_"))
    exe = "initdb.exe" if os.name == "nt" else "initdb"
    initdb = Path(pgserver.__file__).parent / "pginstall" / "bin" / exe
    # --locale=C: the bundled initdb crashes under some Windows locales (Turkish, for one)
    subprocess.run(
        [str(initdb), "-D", str(data), "--auth=trust", "--auth-local=trust",
         "--encoding=utf8", "--locale=C", "-U", "postgres"],
        check=True,
        capture_output=True,
    )  # fmt: skip
    server = pgserver.get_server(data, cleanup_mode="stop")
    try:
        yield str(server.get_uri())
    finally:
        server.cleanup()
        shutil.rmtree(data, ignore_errors=True)


@pytest.fixture(scope="session")
def template_db(pg_uri: str) -> str:
    """A database with the full schema; every test gets its own copy (CREATE ... TEMPLATE)."""
    name = "hastori_template"
    asyncio.run(_admin(pg_uri, f'CREATE DATABASE "{name}"'))
    dsn = with_database(pg_uri, name)
    cfg = alembic_config(dsn)
    command.upgrade(cfg, "0001")
    asyncio.run(_run_sql(dsn, PLAIN_MEASUREMENTS_SQL))
    command.stamp(cfg, "0002")
    command.upgrade(cfg, "head")
    return name


@pytest.fixture
async def db_dsn(pg_uri: str, template_db: str) -> AsyncIterator[str]:
    """DSN of a fresh copy of the schema; dropped afterwards."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    await _admin(pg_uri, f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
    try:
        yield with_database(pg_uri, name)
    finally:
        await _admin(pg_uri, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def pool(db_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(db_dsn, min_size=1, max_size=4)
    assert p is not None
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def fake_redis() -> AsyncIterator["FakeRedisT"]:
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


FakeRedisT = object  # the concrete type comes from the optional fakeredis import


@pytest.fixture
def use_database(db_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the settings (and so scripts, services and Alembic) at this test's database, with
    fixed demo passwords, and return its plain DSN."""
    from hastori_common.settings import get_settings

    monkeypatch.setenv("DATABASE_URL", db_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    monkeypatch.setenv("SEED_SYSTEM_ADMIN_PASSWORD", "admin-test-password")
    monkeypatch.setenv("SEED_SITE_ADMIN_PASSWORD", "siteadmin-test-password")
    monkeypatch.setenv("SEED_VIEWER_PASSWORD", "viewer-test-password")
    get_settings.cache_clear()
    return db_dsn
