"""Time series of one device metric: raw readings, or the 1-minute aggregate (also rolled up to
hours). Plain SQL; `date_trunc` rather than TimescaleDB's `time_bucket` so the query means the
same on any PostgreSQL."""

import uuid
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.errors import ApiError
from hastori_api.queries.devices import get_device
from hastori_api.schemas import Interval, Metric, PointOut, SeriesOut
from hastori_api.scope import SiteScope

MAX_POINTS = 5000
# The 1-minute aggregate is kept 90 days (retention policy), raw readings 7 days.
MAX_RANGE = timedelta(days=90)
RAW_UP_TO = timedelta(hours=2)
MINUTE_UP_TO = timedelta(days=2)

RAW_SQL = text(
    """
    SELECT time, value, NULL::float8 AS min_value, NULL::float8 AS max_value
    FROM measurements
    WHERE device_id = :device AND metric = :metric AND time >= :start AND time < :end
    ORDER BY time
    LIMIT :limit
    """
)
MINUTE_SQL = text(
    """
    SELECT bucket AS time, avg_value AS value, min_value, max_value
    FROM measurements_1m
    WHERE device_id = :device AND metric = :metric AND bucket >= :start AND bucket < :end
    ORDER BY bucket
    LIMIT :limit
    """
)
HOUR_SQL = text(
    """
    SELECT date_trunc('hour', bucket, 'UTC') AS time, avg(avg_value) AS value,
           min(min_value) AS min_value, max(max_value) AS max_value
    FROM measurements_1m
    WHERE device_id = :device AND metric = :metric AND bucket >= :start AND bucket < :end
    GROUP BY 1
    ORDER BY 1
    LIMIT :limit
    """
)
QUERIES = {"raw": RAW_SQL, "1m": MINUTE_SQL, "1h": HOUR_SQL}


def choose_interval(
    start: datetime, end: datetime, requested: Interval
) -> Literal["raw", "1m", "1h"]:
    """`auto` picks the finest resolution that stays readable: raw up to 2 hours, minutes up to
    2 days, hours beyond."""
    if requested != "auto":
        return requested
    span = end - start
    if span <= RAW_UP_TO:
        return "raw"
    return "1m" if span <= MINUTE_UP_TO else "1h"


async def series(
    session: AsyncSession,
    scope: SiteScope,
    device_id: uuid.UUID,
    metric: Metric,
    start: datetime,
    end: datetime,
    requested: Interval,
) -> SeriesOut:
    await get_device(session, scope, device_id)  # the scope check: 404 for another site's device
    if end <= start:
        raise ApiError(422, "`from` must be before `to`", code="validation_error")
    if end - start > MAX_RANGE:
        raise ApiError(
            422,
            "The range is longer than 90 days, as long as the data is kept",
            code="validation_error",
        )
    interval = choose_interval(start, end, requested)
    rows = (
        (
            await session.execute(
                QUERIES[interval],
                {
                    "device": device_id,
                    "metric": metric,
                    "start": start,
                    "end": end,
                    "limit": MAX_POINTS + 1,
                },
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > MAX_POINTS:
        raise ApiError(
            422,
            f"More than {MAX_POINTS} points: narrow the time range or ask for a coarser interval",
            code="validation_error",
        )
    return SeriesOut(
        device_id=device_id,
        metric=metric,
        interval=interval,
        start=start,
        end=end,
        points=[
            PointOut(time=r["time"], value=r["value"], min=r["min_value"], max=r["max_value"])
            for r in rows
        ],
    )
