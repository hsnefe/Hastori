"""Sites, their devices and their daily consumption."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from hastori_api.deps import Scope, Session
from hastori_api.errors import COMMON_ERRORS, ErrorResponse
from hastori_api.queries import consumption, devices, sites
from hastori_api.schemas import ConsumptionOut, DeviceOut, SiteCreate, SiteOut, SitePatch

router = APIRouter(prefix="/sites", tags=["sites"], responses=COMMON_ERRORS)


@router.get("", response_model=list[SiteOut], summary="Sites you can see")
async def list_sites(scope: Scope, session: Session) -> list[SiteOut]:
    return [SiteOut.model_validate(s) for s in await sites.list_sites(session, scope)]


@router.post(
    "",
    response_model=SiteOut,
    status_code=201,
    summary="Add a site (system admin)",
    responses={409: {"model": ErrorResponse, "description": "A site with this name exists"}},
)
async def create_site(body: SiteCreate, scope: Scope, session: Session) -> SiteOut:
    return SiteOut.model_validate(await sites.create_site(session, scope, body))


@router.patch(
    "/{site_id}",
    response_model=SiteOut,
    summary="Change a site (system admin)",
    responses={409: {"model": ErrorResponse, "description": "A site with this name exists"}},
)
async def patch_site(
    site_id: uuid.UUID, body: SitePatch, scope: Scope, session: Session
) -> SiteOut:
    return SiteOut.model_validate(await sites.patch_site(session, scope, site_id, body))


@router.get(
    "/{site_id}/devices",
    response_model=list[DeviceOut],
    summary="Devices of a site with their latest values",
)
async def site_devices(site_id: uuid.UUID, scope: Scope, session: Session) -> list[DeviceOut]:
    return await devices.list_devices(session, scope, site_id)


@router.get(
    "/{site_id}/consumption/daily",
    response_model=ConsumptionOut,
    summary="Daily energy of a site",
    description=(
        "kWh per day from the main panel (energy analyzer) of the site, with the share of the "
        "day's minutes that have data. Days follow the site's time zone."
    ),
)
async def daily_consumption(
    site_id: uuid.UUID,
    scope: Scope,
    session: Session,
    days: Annotated[int, Query(ge=1, le=90, description="how many days, counting today")] = 7,
) -> ConsumptionOut:
    return await consumption.daily(session, scope, site_id, days)
