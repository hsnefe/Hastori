"""Time series of a device."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from hastori_api.deps import Scope, Session
from hastori_api.errors import COMMON_ERRORS, ErrorResponse
from hastori_api.queries import measurements
from hastori_api.schemas import Interval, Metric, SeriesOut
from hastori_api.times import as_utc

router = APIRouter(prefix="/devices", tags=["measurements"], responses=COMMON_ERRORS)

DEFAULT_WINDOW = timedelta(minutes=15)


@router.get(
    "/{device_id}/measurements",
    response_model=SeriesOut,
    summary="Time series of one metric",
    description=(
        "`interval=auto` picks raw readings up to 2 hours, 1-minute averages up to 2 days and "
        "hourly averages beyond. At most 5000 points and 90 days. Times are ISO 8601; one "
        "without an offset is UTC."
    ),
    responses={
        422: {"model": ErrorResponse, "description": "Invalid range or more than 5000 points"}
    },
)
async def device_measurements(
    device_id: uuid.UUID,
    scope: Scope,
    session: Session,
    metric: Annotated[Metric, Query(description="which measurement")],
    start: Annotated[
        datetime | None, Query(alias="from", description="default: 15 minutes before `to`")
    ] = None,
    end: Annotated[datetime | None, Query(alias="to", description="default: now")] = None,
    interval: Annotated[Interval, Query()] = "auto",
) -> SeriesOut:
    end_utc = as_utc(end) if end else datetime.now(UTC)
    start_utc = as_utc(start) if start else end_utc - DEFAULT_WINDOW
    return await measurements.series(
        session, scope, device_id, metric, start_utc, end_utc, interval
    )
