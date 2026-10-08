"""Alarm rules (site admin for their sites, system admin for all)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from hastori_api.deps import Scope, Session
from hastori_api.errors import COMMON_ERRORS, ErrorResponse
from hastori_api.queries import rules
from hastori_api.schemas import MAX_PAGE_SIZE, Page, RuleBody, RuleCreate, RuleOut

router = APIRouter(prefix="/alarm-rules", tags=["alarm-rules"], responses=COMMON_ERRORS)

CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorResponse,
        "description": "The device already has an enabled rule of this kind for this metric",
    }
}


@router.get("", response_model=Page[RuleOut], summary="Rules of your sites")
async def list_rules(
    scope: Scope,
    session: Session,
    site_id: Annotated[uuid.UUID | None, Query()] = None,
    device_id: Annotated[uuid.UUID | None, Query()] = None,
    enabled: Annotated[bool | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> Page[RuleOut]:
    items, total = await rules.list_rules(
        session, scope, site_id=site_id, device_id=device_id, enabled=enabled, page=page, size=size
    )
    return Page[RuleOut](items=items, total=total, page=page, size=size)


@router.post(
    "",
    response_model=RuleOut,
    status_code=201,
    summary="Create a rule",
    description=(
        "`clear_threshold` is required: the alarm opens above `threshold` (for '>') after "
        "`duration_s` seconds and closes only below `clear_threshold`. A `reactive_ratio` rule "
        "watches the ratio of reactive to active energy over `window_s` seconds and belongs on "
        "an energy analyzer. A `no_data` rule opens when `metric` has not arrived for "
        "`duration_s` seconds (10 to 600) and needs no threshold fields; it stays quiet while "
        "the whole pipeline is silent, so an outage of the broker or ingestion is not blamed on "
        "the device."
    ),
    responses=CONFLICT,
)
async def create_rule(body: RuleCreate, scope: Scope, session: Session) -> RuleOut:
    return await rules.create_rule(session, scope, body)


@router.get("/{rule_id}", response_model=RuleOut, summary="One rule")
async def get_rule(rule_id: uuid.UUID, scope: Scope, session: Session) -> RuleOut:
    return await rules.get_rule(session, scope, rule_id)


@router.put("/{rule_id}", response_model=RuleOut, summary="Replace a rule", responses=CONFLICT)
async def update_rule(
    rule_id: uuid.UUID, body: RuleBody, scope: Scope, session: Session
) -> RuleOut:
    return await rules.update_rule(session, scope, rule_id, body)


@router.delete(
    "/{rule_id}",
    status_code=204,
    summary="Disable a rule",
    description=(
        "Rules are kept (alarms refer to them): the rule stops evaluating and its open alarm "
        "is closed. `PUT` with `enabled: true` turns it back on."
    ),
)
async def delete_rule(rule_id: uuid.UUID, scope: Scope, session: Session) -> Response:
    await rules.disable_rule(session, scope, rule_id)
    return Response(status_code=204)
