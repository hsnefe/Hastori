"""Device queries, always through the SiteScope."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.errors import not_found
from hastori_api.schemas import DeviceOut, LatestValue
from hastori_api.scope import SiteScope
from hastori_common.models import Device

ONLINE_WITHIN = timedelta(seconds=30)

# One row per (device, metric): the newest reading of the last 10 minutes. Served by the unique
# index (device_id, metric, time DESC), so it does not scan the table.
LATEST_SQL = text(
    """
    SELECT DISTINCT ON (device_id, metric) device_id, metric, value, time
    FROM measurements
    WHERE device_id = ANY(:ids) AND time > now() - interval '10 minutes'
    ORDER BY device_id, metric, time DESC
    """
)


async def get_device(session: AsyncSession, scope: SiteScope, device_id: uuid.UUID) -> Device:
    """A device of one of the user's sites, or 404 (also for a device of somebody else's site)."""
    stmt = select(Device).where(Device.id == device_id, Device.site_id.in_(scope.site_ids))
    device = await session.scalar(stmt)
    if device is None:
        raise not_found("Device not found")
    return device


async def list_devices(
    session: AsyncSession, scope: SiteScope, site_id: uuid.UUID
) -> list[DeviceOut]:
    scope.require_site(site_id)
    devices = list(
        await session.scalars(select(Device).where(Device.site_id == site_id).order_by(Device.name))
    )
    latest: dict[uuid.UUID, dict[str, LatestValue]] = {d.id: {} for d in devices}
    if devices:
        rows = (await session.execute(LATEST_SQL, {"ids": [d.id for d in devices]})).mappings()
        for r in rows:
            latest[r["device_id"]][r["metric"]] = LatestValue(value=r["value"], time=r["time"])
    now = datetime.now(UTC)
    out = []
    for d in devices:
        values = latest[d.id]
        seen = max((v.time for v in values.values()), default=None)
        out.append(
            DeviceOut(
                id=d.id,
                site_id=d.site_id,
                key=d.key,
                name=d.name,
                type=d.type,
                is_active=d.is_active,
                online=seen is not None and now - seen <= ONLINE_WITHIN,
                last_seen=seen,
                latest=values,
            )
        )
    return out
