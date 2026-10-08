"""Migrations 0003-0006 against a real PostgreSQL (0002 is replaced by a plain stand-in, see
conftest.py). The point: the SQL is valid, the constraints do what the comments say, downgrades
work, and the models match the database (`alembic check`)."""

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command

from conftest import alembic_config, with_database

ORG = uuid.UUID(int=1)
SITE = uuid.UUID(int=2)
DEVICE = uuid.UUID(int=3)


async def seed_device(conn: asyncpg.Connection) -> None:
    await conn.execute("INSERT INTO organizations (id, name) VALUES ($1, 'org')", ORG)
    await conn.execute("INSERT INTO sites (id, org_id, name) VALUES ($1, $2, 's')", SITE, ORG)
    await conn.execute(
        "INSERT INTO devices (id, site_id, key, name, type, mqtt_topic) "
        "VALUES ($1, $2, 'k', 'n', 'compressor', 't')",
        DEVICE,
        SITE,
    )


RULE = (
    "INSERT INTO alarm_rules (id, device_id, name, metric, operator, threshold, duration_s, "
    "clear_threshold, severity {extra}) VALUES ($1, $2, 'r', $3, $4, $5, 30, $6, 'critical' {vals})"
)


async def add_rule(conn: asyncpg.Connection, metric: str = "temperature_c", **kw: object) -> None:
    extra, vals, args = "", "", []
    for i, (col, value) in enumerate(kw.items()):
        extra += f", {col}"
        vals += f", ${7 + i}"
        args.append(value)
    await conn.execute(
        RULE.format(extra=extra, vals=vals),
        uuid.uuid4(),
        DEVICE,
        metric,
        ">",
        80.0,
        75.0,
        *args,
    )


async def test_constraints_of_0005(db_dsn: str) -> None:
    conn = await asyncpg.connect(db_dsn)
    try:
        await seed_device(conn)
        await add_rule(conn)  # a plain threshold rule

        with pytest.raises(asyncpg.CheckViolationError):  # a ratio rule needs a window
            await add_rule(conn, "reactive_power_kvar", kind="reactive_ratio")
        with pytest.raises(asyncpg.CheckViolationError):  # a window on a threshold rule
            await add_rule(conn, "current_a", window_s=600)
        with pytest.raises(asyncpg.CheckViolationError):  # a ratio rule watches reactive power
            await add_rule(conn, "temperature_c", kind="reactive_ratio", window_s=600)
        with pytest.raises(asyncpg.CheckViolationError):  # window out of range
            await add_rule(conn, "reactive_power_kvar", kind="reactive_ratio", window_s=30)
        with pytest.raises(asyncpg.NotNullViolationError):  # hysteresis is mandatory
            await conn.execute(
                "INSERT INTO alarm_rules (id, device_id, name, metric, operator, threshold, "
                "duration_s, severity) VALUES ($1, $2, 'r', 'current_a', '>', 90, 60, 'warning')",
                uuid.uuid4(),
                DEVICE,
            )
        await add_rule(conn, "reactive_power_kvar", kind="reactive_ratio", window_s=600)

        # one enabled rule per device, metric and kind
        with pytest.raises(asyncpg.UniqueViolationError):
            await add_rule(conn)
        await add_rule(conn, enabled=False)  # a disabled duplicate is fine
    finally:
        await conn.close()


async def test_one_open_alarm_per_rule(db_dsn: str) -> None:
    conn = await asyncpg.connect(db_dsn)
    try:
        await seed_device(conn)
        rule_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO alarm_rules (id, device_id, name, metric, operator, threshold, "
            "duration_s, clear_threshold, severity) VALUES "
            "($1, $2, 'r', 'temperature_c', '>', 80, 30, 75, 'critical')",
            rule_id,
            DEVICE,
        )
        sql = (
            "INSERT INTO alarms (id, rule_id, device_id, state, opened_at) "
            "VALUES ($1, $2, $3, $4, now()) "
            "ON CONFLICT (rule_id) WHERE state IN ('active', 'acknowledged') DO NOTHING "
            "RETURNING id"
        )
        first = await conn.fetchval(sql, uuid.uuid4(), rule_id, DEVICE, "active")
        second = await conn.fetchval(sql, uuid.uuid4(), rule_id, DEVICE, "active")
        assert first is not None and second is None
        await conn.execute("UPDATE alarms SET state = 'cleared', cleared_at = now()")
        assert await conn.fetchval(sql, uuid.uuid4(), rule_id, DEVICE, "active") is not None
    finally:
        await conn.close()


async def test_rule_changes_notify(db_dsn: str) -> None:
    conn = await asyncpg.connect(db_dsn)
    listener = await asyncpg.connect(db_dsn)
    seen: list[str] = []
    try:
        await seed_device(conn)
        await listener.add_listener("alarm_rules_changed", lambda *a: seen.append(a[3]))
        rule_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO alarm_rules (id, device_id, name, metric, operator, threshold, "
            "duration_s, clear_threshold, severity) VALUES "
            "($1, $2, 'r', 'temperature_c', '>', 80, 30, 75, 'critical')",
            rule_id,
            DEVICE,
        )
        await conn.execute("UPDATE alarm_rules SET threshold = 85 WHERE id = $1", rule_id)
        await conn.execute("DELETE FROM alarm_rules WHERE id = $1", rule_id)
        for _ in range(50):
            if len(seen) >= 3:
                break
            await asyncio.sleep(0.05)
        assert seen == [str(rule_id)] * 3  # insert, update and delete all name the rule
    finally:
        await listener.close()
        await conn.close()


async def test_sites_have_a_default_time_zone(db_dsn: str) -> None:
    conn = await asyncpg.connect(db_dsn)
    try:
        await seed_device(conn)
        assert await conn.fetchval("SELECT timezone FROM sites") == "Europe/Istanbul"
    finally:
        await conn.close()


def test_backfill_gives_old_rules_a_five_percent_band(pg_uri: str, template_db: str) -> None:
    """Upgrade a database that still has rules without a clear threshold."""
    name = f"t_{uuid.uuid4().hex[:12]}"

    async def prepare() -> None:
        conn = await asyncpg.connect(with_database(pg_uri, "postgres"))
        await conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
        await conn.close()

    asyncio.run(prepare())
    dsn = with_database(pg_uri, name)
    cfg = alembic_config(dsn)
    command.downgrade(cfg, "0004")

    async def insert_old_rules() -> None:
        conn = await asyncpg.connect(dsn)
        await seed_device(conn)
        for op, thr, metric in ((">", 90.0, "current_a"), ("<", 10.0, "active_power_kw")):
            await conn.execute(
                "INSERT INTO alarm_rules (id, device_id, name, metric, operator, threshold, "
                "duration_s, severity) VALUES ($1, $2, 'old', $3, $4, $5, 60, 'warning')",
                uuid.uuid4(),
                DEVICE,
                metric,
                op,
                thr,
            )
        await conn.close()

    asyncio.run(insert_old_rules())
    command.upgrade(cfg, "head")

    async def read() -> dict[str, float]:
        conn = await asyncpg.connect(dsn)
        rows = await conn.fetch("SELECT operator, clear_threshold FROM alarm_rules")
        await conn.close()
        return {r["operator"]: r["clear_threshold"] for r in rows}

    got = asyncio.run(read())
    assert got[">"] == pytest.approx(85.5) and got["<"] == pytest.approx(10.5)


def test_downgrade_and_upgrade_again(pg_uri: str, template_db: str) -> None:
    name = f"t_{uuid.uuid4().hex[:12]}"

    async def prepare() -> None:
        conn = await asyncpg.connect(with_database(pg_uri, "postgres"))
        await conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
        await conn.close()

    asyncio.run(prepare())
    cfg = alembic_config(with_database(pg_uri, name))
    command.downgrade(cfg, "0003")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002")  # 0003-0006 all the way down (0002 itself needs TimescaleDB)
    command.upgrade(cfg, "head")


def test_models_match_the_migrated_database(pg_uri: str, template_db: str) -> None:
    """`alembic check` finds no difference between the ORM models and the schema."""
    cfg = alembic_config(with_database(pg_uri, template_db))
    command.check(cfg)


def test_downgrading_switches_reactive_ratio_rules_off(pg_uri: str, template_db: str) -> None:
    """Without `kind` they would read as a threshold on reactive power and alarm forever."""
    name = f"t_{uuid.uuid4().hex[:12]}"

    async def prepare() -> None:
        conn = await asyncpg.connect(with_database(pg_uri, "postgres"))
        await conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
        await conn.close()
        conn = await asyncpg.connect(with_database(pg_uri, name))
        await seed_device(conn)
        await conn.execute("UPDATE devices SET type = 'energy_analyzer' WHERE id = $1", DEVICE)
        await conn.execute(
            "INSERT INTO alarm_rules (id, device_id, name, metric, operator, threshold, "
            "clear_threshold, duration_s, severity, kind, window_s) VALUES "
            "($1, $2, 'ratio', 'reactive_power_kvar', '>', 0.18, 0.165, 0, 'warning', "
            "'reactive_ratio', 600)",
            uuid.uuid4(),
            DEVICE,
        )
        await conn.close()

    asyncio.run(prepare())
    dsn = with_database(pg_uri, name)
    command.downgrade(alembic_config(dsn), "0004")

    async def enabled() -> bool:
        conn = await asyncpg.connect(dsn)
        try:
            return bool(await conn.fetchval("SELECT enabled FROM alarm_rules"))
        finally:
            await conn.close()

    assert asyncio.run(enabled()) is False


def test_0006_adds_the_thresholds_and_downgrades_cleanly(pg_uri: str, template_db: str) -> None:
    name = f"t_{uuid.uuid4().hex[:12]}"

    async def prepare() -> None:
        conn = await asyncpg.connect(with_database(pg_uri, "postgres"))
        await conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
        await conn.close()

    asyncio.run(prepare())
    dsn = with_database(pg_uri, name)
    cfg = alembic_config(dsn)

    async def columns() -> set[str]:
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'alarms'"
            )
            return {r["column_name"] for r in rows}
        finally:
            await conn.close()

    assert {"threshold", "clear_threshold"} <= asyncio.run(columns())
    command.downgrade(cfg, "0005")
    assert not {"threshold", "clear_threshold"} & asyncio.run(columns())
    command.upgrade(cfg, "head")
    assert {"threshold", "clear_threshold"} <= asyncio.run(columns())
