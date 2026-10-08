"""Daily energy of a site, from the 1-minute aggregate.

The main panel (energy analyzer) already measures everything behind it, so only analyzers are
summed: adding the compressors as well would count their energy twice. Energy is the integral of
power; each 1-minute bucket holds the average kW of that minute, i.e. kW / 60 kWh. A minute without
data contributes nothing, and `coverage` says how much of the day that is: a total with a hole is
reported as a total with a hole, never as a complete day.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.errors import ApiError
from hastori_api.queries.sites import get_site
from hastori_api.schemas import ConsumptionOut, DayConsumption
from hastori_api.scope import SiteScope
from hastori_common.models import Device

# The day boundaries come in as UTC ranges computed in Python (zoneinfo): one source of truth for
# time zones, the same one `minutes_in_day` uses, and nothing here depends on the database knowing
# the zone name (or its tz database being the same version).
DAILY_SQL = text(
    """
    SELECT d.day,
           coalesce(sum(m.avg_value) FILTER (WHERE m.metric = 'active_power_kw'), 0) / 60.0 AS kwh,
           coalesce(sum(greatest(m.avg_value, 0))
                    FILTER (WHERE m.metric = 'reactive_power_kvar'), 0) / 60.0 AS kvarh,
           count(*) FILTER (WHERE m.metric = 'active_power_kw') AS minutes
    FROM unnest(CAST(:days AS date[]), CAST(:starts AS timestamptz[]),
                CAST(:ends AS timestamptz[])) AS d(day, start_utc, end_utc)
    JOIN measurements_1m m ON m.bucket >= d.start_utc AND m.bucket < d.end_utc
    JOIN devices dev ON dev.id = m.device_id
    WHERE dev.site_id = :site AND dev.type = 'energy_analyzer' AND dev.is_active
      AND m.bucket >= :first_start AND m.bucket < :last_end
    GROUP BY d.day
    """
)


def local_midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone)


def minutes_in_day(day: date, zone: ZoneInfo, now: datetime) -> int:
    """Minutes of that local day that have already happened (a day is not always 24 hours)."""
    start = local_midnight(day, zone).astimezone(UTC)
    end = local_midnight(day + timedelta(days=1), zone).astimezone(UTC)
    return max(0, int((min(end, now.astimezone(UTC)) - start).total_seconds() // 60))


async def daily(
    session: AsyncSession,
    scope: SiteScope,
    site_id: uuid.UUID,
    days: int,
    now: datetime | None = None,
) -> ConsumptionOut:
    site = await get_site(session, scope, site_id)  # 404 outside the scope
    try:
        zone = ZoneInfo(site.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # the API validates the value, but the column and the seed file are other ways in
        raise ApiError(
            422,
            f"The site's time zone {site.timezone!r} is not valid: correct it with PATCH /sites",
        ) from None
    now = now or datetime.now(UTC)
    today = now.astimezone(zone).date()
    first = today - timedelta(days=days - 1)

    panels = int(
        await session.scalar(
            select(func.count())
            .select_from(Device)
            .where(
                Device.site_id == site_id,
                Device.type == "energy_analyzer",
                Device.is_active.is_(True),
            )
        )
        or 0
    )
    if panels == 0:
        return ConsumptionOut(site_id=site_id, timezone=site.timezone, days=[])

    wanted = [first + timedelta(days=i) for i in range(days)]
    starts = [local_midnight(d, zone).astimezone(UTC) for d in wanted]
    ends = [local_midnight(d + timedelta(days=1), zone).astimezone(UTC) for d in wanted]
    rows = (
        await session.execute(
            DAILY_SQL,
            {
                "days": wanted,
                "starts": starts,
                "ends": ends,
                "site": site_id,
                "first_start": starts[0],
                "last_end": ends[-1],
            },
        )
    ).mappings()
    by_day = {r["day"]: r for r in rows}

    out: list[DayConsumption] = []
    for day in wanted:
        row = by_day.get(day)
        expected = minutes_in_day(day, zone, now) * panels
        kwh = float(row["kwh"]) if row else 0.0
        kvarh = float(row["kvarh"]) if row else 0.0
        minutes = int(row["minutes"]) if row else 0
        out.append(
            DayConsumption(
                date=day,
                kwh=round(kwh, 3),
                coverage=round(min(1.0, minutes / expected), 4) if expected else 0.0,
                reactive_kvarh=round(kvarh, 3),
                reactive_ratio=round(kvarh / kwh, 4) if kwh > 0 else None,
            )
        )
    return ConsumptionOut(site_id=site_id, timezone=site.timezone, days=out)
