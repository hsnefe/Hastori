"""Idempotent seed: loads seed/demo.yaml into the database (ON CONFLICT (id) DO UPDATE)."""

import asyncio
import os
from typing import Any

from pwdlib import PasswordHash
from sqlalchemy import func, select
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


def _upsert(table: Any, rows: list[dict[str, Any]]) -> Any:
    stmt = insert(table).values(rows)
    updates = {c.name: stmt.excluded[c.name] for c in table.__table__.columns if not c.primary_key}
    updates.pop("created_at", None)
    return stmt.on_conflict_do_update(index_elements=["id"], set_=updates)


async def main() -> None:
    settings = get_settings()
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
        }
        for r in seed.alarm_rules
    ]

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(_upsert(Organization, [{"id": org_id, "name": seed.organization.name}]))
        await conn.execute(
            _upsert(
                Site,
                [{"id": s.id, "org_id": org_id, "name": s.name} for s in seed.sites],
            )
        )
        await conn.execute(_upsert(User, users))
        if user_sites:
            await conn.execute(insert(UserSite).values(user_sites).on_conflict_do_nothing())
        await conn.execute(_upsert(Device, devices))
        await conn.execute(_upsert(AlarmRule, rules))

        counts = {}
        for model in (Organization, Site, User, UserSite, Device, AlarmRule):
            counts[model.__tablename__] = await conn.scalar(select(func.count()).select_from(model))
    await engine.dispose()
    print("seeded:", counts)


if __name__ == "__main__":
    asyncio.run(main())
