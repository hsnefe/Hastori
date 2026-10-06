"""Alarm history and acknowledgement."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request

from hastori_api.deps import Scope, Session
from hastori_api.errors import COMMON_ERRORS, ErrorResponse
from hastori_api.events import publish_acknowledged
from hastori_api.queries import alarms
from hastori_api.schemas import MAX_PAGE_SIZE, AlarmDetailOut, AlarmOut, AlarmState, Page
from hastori_api.times import as_utc

router = APIRouter(prefix="/alarms", tags=["alarms"], responses=COMMON_ERRORS)


@router.get(
    "",
    response_model=Page[AlarmOut],
    summary="Alarms of your sites, newest first",
)
async def list_alarms(
    scope: Scope,
    session: Session,
    state: Annotated[AlarmState | None, Query()] = None,
    site_id: Annotated[uuid.UUID | None, Query()] = None,
    start: Annotated[datetime | None, Query(alias="from", description="opened at or after")] = None,
    end: Annotated[datetime | None, Query(alias="to", description="opened before")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> Page[AlarmOut]:
    items, total = await alarms.list_alarms(
        session,
        scope,
        state=state,
        site_id=site_id,
        start=as_utc(start) if start else None,
        end=as_utc(end) if end else None,
        page=page,
        size=size,
    )
    return Page[AlarmOut](items=items, total=total, page=page, size=size)


@router.get("/{alarm_id}", response_model=AlarmDetailOut, summary="One alarm with its timeline")
async def get_alarm(alarm_id: uuid.UUID, scope: Scope, session: Session) -> AlarmDetailOut:
    return await alarms.alarm_detail(session, scope, alarm_id)


@router.post(
    "/{alarm_id}/ack",
    response_model=AlarmOut,
    summary="Acknowledge an active alarm (site admin, system admin)",
    description=(
        "Records who acknowledged and when. The alarm closes by itself when the value recovers."
    ),
    responses={409: {"model": ErrorResponse, "description": "The alarm is not active (any more)"}},
)
async def acknowledge(
    alarm_id: uuid.UUID, request: Request, scope: Scope, session: Session
) -> AlarmOut:
    out = await alarms.acknowledge(session, scope, alarm_id)
    await publish_acknowledged(request.app.state.redis, out)
    return out
