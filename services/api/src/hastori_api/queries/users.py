"""User management: system admin only, within their own organisation."""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.audit import audit
from hastori_api.errors import ApiError, conflict, not_found
from hastori_api.schemas import UserCreate, UserOut, UserPatch
from hastori_api.scope import SYSTEM_ADMIN, SiteScope
from hastori_api.security import hash_password_async
from hastori_common.models import User, UserSite


def _unknown_sites(scope: SiteScope, site_ids: list[uuid.UUID]) -> ApiError | None:
    """A system admin sees every site of the organisation, so the scope is the allowed set."""
    unknown = sorted(str(s) for s in set(site_ids) - scope.site_ids)
    if unknown:
        return ApiError(422, f"Unknown sites: {', '.join(unknown)}", code="validation_error")
    return None


async def _sites_of(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = {u: [] for u in user_ids}
    if user_ids:
        rows = await session.execute(select(UserSite).where(UserSite.user_id.in_(user_ids)))
        for (link,) in rows:
            out[link.user_id].append(link.site_id)
    return {u: sorted(s) for u, s in out.items()}


def _out(user: User, site_ids: list[uuid.UUID]) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,  # type: ignore[arg-type]
        site_ids=site_ids,
        created_at=user.created_at,
    )


async def list_users(
    session: AsyncSession, scope: SiteScope, page: int, size: int
) -> tuple[list[UserOut], int]:
    scope.require_role(SYSTEM_ADMIN)
    base = select(User).where(User.org_id == scope.org_id)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    users = list(
        await session.scalars(
            base.order_by(func.lower(User.email)).limit(size).offset((page - 1) * size)
        )
    )
    sites = await _sites_of(session, [u.id for u in users])
    return [_out(u, sites[u.id]) for u in users], total


async def _get(session: AsyncSession, scope: SiteScope, user_id: uuid.UUID) -> User:
    user = await session.scalar(select(User).where(User.id == user_id, User.org_id == scope.org_id))
    if user is None:
        raise not_found("User not found")
    return user


async def get_user(session: AsyncSession, scope: SiteScope, user_id: uuid.UUID) -> UserOut:
    scope.require_role(SYSTEM_ADMIN)
    user = await _get(session, scope, user_id)
    return _out(user, (await _sites_of(session, [user.id]))[user.id])


async def create_user(session: AsyncSession, scope: SiteScope, body: UserCreate) -> UserOut:
    scope.require_role(SYSTEM_ADMIN)
    if err := _unknown_sites(scope, body.site_ids):
        raise err
    email = body.email.strip()
    taken = await session.scalar(select(User.id).where(func.lower(User.email) == email.lower()))
    if taken is not None:
        raise conflict("A user with this e-mail address already exists")
    password_hash = await hash_password_async(body.password)
    user = User(org_id=scope.org_id, email=email, password_hash=password_hash, role=body.role)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:  # two requests with the same address at once: the unique index decides
        await session.rollback()
        raise conflict("A user with this e-mail address already exists") from None
    site_ids = [] if body.role == "system_admin" else sorted(set(body.site_ids))
    session.add_all(UserSite(user_id=user.id, site_id=s) for s in site_ids)
    audit(
        session,
        scope,
        "user.create",
        "user",
        user.id,
        {"email": email, "role": body.role, "site_ids": site_ids},
    )
    await session.commit()
    return _out(user, site_ids)


async def patch_user(
    session: AsyncSession, scope: SiteScope, user_id: uuid.UUID, body: UserPatch
) -> UserOut:
    scope.require_role(SYSTEM_ADMIN)
    user = await _get(session, scope, user_id)
    changed: dict[str, object] = {}

    if body.role is not None and body.role != user.role:
        if user.role == SYSTEM_ADMIN:
            others = await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.org_id == scope.org_id, User.role == SYSTEM_ADMIN, User.id != user.id)
            )
            if not others:
                raise conflict("The last system admin cannot be demoted")
        user.role = body.role
        changed["role"] = body.role

    if body.site_ids is not None:
        if err := _unknown_sites(scope, body.site_ids):
            raise err
        wanted = [] if user.role == "system_admin" else sorted(set(body.site_ids))
        await session.execute(delete(UserSite).where(UserSite.user_id == user.id))
        session.add_all(UserSite(user_id=user.id, site_id=s) for s in wanted)
        changed["site_ids"] = wanted
    elif body.role == "system_admin":
        await session.execute(delete(UserSite).where(UserSite.user_id == user.id))

    if body.password is not None:
        user.password_hash = await hash_password_async(body.password)
        changed["password"] = "changed"  # the fact, never the value

    audit(session, scope, "user.update", "user", user.id, changed)
    await session.commit()
    return _out(user, (await _sites_of(session, [user.id]))[user.id])
