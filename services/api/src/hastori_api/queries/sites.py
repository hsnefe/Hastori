"""Site queries. Every function takes the SiteScope and filters by it: that is the only way to
read sites, so a route cannot forget the tenant filter."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.scope import SiteScope
from hastori_common.models import Site


async def list_sites(session: AsyncSession, scope: SiteScope) -> list[Site]:
    stmt = select(Site).where(Site.id.in_(scope.site_ids)).order_by(Site.name)
    return list((await session.scalars(stmt)).all())


async def get_site(session: AsyncSession, scope: SiteScope, site_id: uuid.UUID) -> Site:
    scope.require_site(site_id)
    site = await session.get(Site, site_id)
    assert site is not None  # in the scope, so it exists
    return site


async def count_sites(session: AsyncSession, scope: SiteScope) -> int:
    stmt = select(func.count()).select_from(Site).where(Site.id.in_(scope.site_ids))
    return int(await session.scalar(stmt) or 0)
