"""Idempotent seed: loads seed/demo.yaml into the database.

By default it never overwrites what people changed in the running system: existing users keep
their e-mail address, role and password hash, existing sites keep name, city and time zone,
devices keep their name and active flag, and existing alarm rules keep their settings.
`--reset` makes the YAML win again (demo reset): passwords are re-hashed from the environment,
rules are restored and the site assignments of the seeded users are reconciled. Users and
devices that were removed from the YAML are never deleted (history and audit records
reference them).
"""

import argparse
import asyncio
import os
from typing import Any

from pwdlib import PasswordHash
from sqlalchemy import ColumnElement, and_, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import create_async_engine

from hastori_common.models import (
    AlarmRule,
    Device,
    Organization,
    Site,
    User,
    UserSite,
)
from hastori_common.seed_data import load_seed
from hastori_common.settings import get_settings


def _upsert(table: Any, rows: list[dict[str, Any]], keep: set[str] | None = None) -> Any:
    """INSERT ... ON CONFLICT (id) DO UPDATE, leaving the columns in `keep` untouched."""
    stmt = insert(table).values(rows)
    skip = {"created_at"} | (keep or set())
    updates = {
        c.name: stmt.excluded[c.name]
        for c in table.__table__.columns
        if not c.primary_key and c.name not in skip
    }
    return stmt.on_conflict_do_update(index_elements=["id"], set_=updates)


async def main(reset: bool) -> None:
    settings = get_settings(strict=("database_url",))
    seed = load_seed()
    hasher = PasswordHash.recommended()
    org_id = seed.organization.id

    users = []
    for u in seed.users:
        password = os.environ.get(u.password_env.upper()) or getattr(
            settings, u.password_env.lower()
        )
        users.append(
            {
                "id": u.id,
                "org_id": org_id,
                "email": u.email,
                "role": u.role,
                "password_hash": hasher.hash(password),
            }
        )
    user_sites = [
        {"user_id": u.id, "site_id": seed.site_by_key(s).id} for u in seed.users for s in u.sites
    ]
    devices = [
        {
            "id": d.id,
            "site_id": seed.site_by_key(d.site).id,
            "key": d.key,
            "name": d.name,
            "type": d.type,
            "mqtt_topic": seed.topic(d),
        }
        for d in seed.devices
    ]
    rules = [
        {
            "id": r.id,
            "device_id": seed.device_by_key(r.device).id,
            "name": r.name,
            "metric": r.metric,
            "operator": r.operator,
            "threshold": r.threshold,
            "duration_s": r.duration_s,
            "clear_threshold": r.clear_threshold,
            "severity": r.severity,
            "kind": r.kind,
            "window_s": r.window_s,
        }
        for r in seed.alarm_rules
    ]

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(_upsert(Organization, [{"id": org_id, "name": seed.organization.name}]))
        sites = [
            {
                "id": s.id,
                "org_id": org_id,
                "name": s.name,
                "city": s.city,
                "timezone": s.timezone,
            }
            for s in seed.sites
        ]
        # Everything the API can change (site name, city, time zone; user e-mail, role, password;
        # device name) is kept by a plain re-seed; `--reset` is the way back to the YAML.
        await conn.execute(
            _upsert(Site, sites, keep=set() if reset else {"name", "city", "timezone"})
        )
        await conn.execute(
            _upsert(User, users, keep=set() if reset else {"email", "role", "password_hash"})
        )
        if user_sites:
            await conn.execute(insert(UserSite).values(user_sites).on_conflict_do_nothing())
        if reset:
            # YAML wins: a seeded user keeps only the sites the YAML gives them.
            wanted = [(r["user_id"], r["site_id"]) for r in user_sites]
            stale: ColumnElement[bool] = UserSite.user_id.in_([u["id"] for u in users])
            if wanted:
                stale = and_(stale, tuple_(UserSite.user_id, UserSite.site_id).not_in(wanted))
            await conn.execute(delete(UserSite).where(stale))
        await conn.execute(
            _upsert(Device, devices, keep={"is_active"} if reset else {"is_active", "name"})
        )
        if reset:
            await conn.execute(_upsert(AlarmRule, rules))
        else:
            await conn.execute(insert(AlarmRule).values(rules).on_conflict_do_nothing())

        counts = {}
        for model in (Organization, Site, User, UserSite, Device, AlarmRule):
            counts[model.__tablename__] = await conn.scalar(select(func.count()).select_from(model))
    await engine.dispose()
    print("seeded:", counts, "(reset)" if reset else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset", action="store_true", help="overwrite user passwords and alarm rules from YAML"
    )
    asyncio.run(main(parser.parse_args().reset))
