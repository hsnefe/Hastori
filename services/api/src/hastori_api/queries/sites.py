"""Site queries. Every function takes the SiteScope and filters by it: that is the only way to
read sites, so a route cannot forget the tenant filter."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.audit import audit
from hastori_api.errors import conflict
from hastori_api.schemas import SiteCreate, SitePatch
from hastori_api.scope import SYSTEM_ADMIN, SiteScope
from hastori_common.models import Site


async def list_sites(session: AsyncSession, scope: SiteScope) -> list[Site]:
    stmt = select(Site).where(Site.id.in_(scope.site_ids)).order_by(Site.name)
    return list((await session.scalars(stmt)).all())


async def get_site(session: AsyncSession, scope: SiteScope, site_id: uuid.UUID) -> Site:
    scope.require_site(site_id)
    site = await session.get(Site, site_id)
    assert site is not None  # in the scope, so it exists
    return site


async def create_site(session: AsyncSession, scope: SiteScope, body: SiteCreate) -> Site:
    scope.require_role(SYSTEM_ADMIN)
    site = Site(org_id=scope.org_id, name=body.name, city=body.city, timezone=body.timezone)
    session.add(site)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise conflict("A site with this name already exists") from None
    audit(session, scope, "site.create", "site", site.id, body.model_dump())
    await session.commit()
    return site


async def patch_site(
    session: AsyncSession, scope: SiteScope, site_id: uuid.UUID, body: SitePatch
) -> Site:
    scope.require_role(SYSTEM_ADMIN)
    site = await get_site(session, scope, site_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=False)
    for field, value in changes.items():
        if value is None and field != "city":
            continue  # name and timezone cannot be emptied
        setattr(site, field, value)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise conflict("A site with this name already exists") from None
    audit(session, scope, "site.update", "site", site.id, changes)
    await session.commit()
    return site
