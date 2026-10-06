"""scripts/seed.py against a real database: idempotent, protective by default, `--reset` wins."""

import importlib.util
from pathlib import Path
from types import ModuleType

import asyncpg

from hastori_common.seed_data import load_seed

ROOT = Path(__file__).resolve().parents[1]


def load_seed_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_script", ROOT / "scripts" / "seed.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def snapshot(dsn: str) -> dict[str, list[tuple[object, ...]]]:
    conn = await asyncpg.connect(dsn)
    try:
        out = {}
        for name, query in {
            "sites": "SELECT * FROM sites ORDER BY id",
            "users": "SELECT id, email, role, password_hash FROM users ORDER BY id",
            "rules": "SELECT * FROM alarm_rules ORDER BY id",
            "user_sites": "SELECT * FROM user_sites ORDER BY 1, 2",
        }.items():
            out[name] = [tuple(r.values()) for r in await conn.fetch(query)]
        return out
    finally:
        await conn.close()


async def test_seed_loads_everything_and_is_idempotent(use_database: str) -> None:
    seed = load_seed_script()
    await seed.main(reset=False)
    first = await snapshot(use_database)
    assert len(first["rules"]) == len(load_seed().alarm_rules) == 4
    assert len(first["users"]) == 5

    await seed.main(reset=False)
    assert await snapshot(use_database) == first


async def test_a_plain_seed_keeps_what_people_changed(use_database: str) -> None:
    seed = load_seed_script()
    await seed.main(reset=False)
    conn = await asyncpg.connect(use_database)
    await conn.execute(
        "UPDATE alarm_rules SET threshold = 99, clear_threshold = 90 WHERE name LIKE 'Kompres%'"
    )
    await conn.execute("UPDATE users SET password_hash = 'changed' WHERE role = 'system_admin'")
    await conn.execute("UPDATE sites SET timezone = 'Europe/Berlin'")
    await conn.close()

    await seed.main(reset=False)
    conn = await asyncpg.connect(use_database)
    assert await conn.fetchval("SELECT threshold FROM alarm_rules WHERE name LIKE 'Kompres%'") == 99
    assert (
        await conn.fetchval("SELECT password_hash FROM users WHERE role = 'system_admin'")
        == "changed"
    )
    assert set(await conn.fetchval("SELECT array_agg(DISTINCT timezone) FROM sites")) == {
        "Europe/Berlin"
    }

    await seed.main(reset=True)  # YAML wins again
    assert await conn.fetchval("SELECT threshold FROM alarm_rules WHERE name LIKE 'Kompres%'") == 80
    assert (
        await conn.fetchval("SELECT password_hash FROM users WHERE role = 'system_admin'")
        != "changed"
    )
    assert set(await conn.fetchval("SELECT array_agg(DISTINCT timezone) FROM sites")) == {
        "Europe/Istanbul"
    }
    await conn.close()


async def test_the_seed_satisfies_the_database_constraints(use_database: str) -> None:
    """The rules, including the two reactive-ratio ones, are accepted by the CHECK constraints
    and the unique index (an insert would raise otherwise)."""
    await load_seed_script().main(reset=False)
    conn = await asyncpg.connect(use_database)
    rows = await conn.fetch("SELECT kind, window_s, clear_threshold FROM alarm_rules ORDER BY kind")
    await conn.close()
    assert [r["kind"] for r in rows].count("reactive_ratio") == 2
    assert all(r["clear_threshold"] is not None for r in rows)
