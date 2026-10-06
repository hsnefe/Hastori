"""Request dependencies: database session, the signed-in user's scope, role checks."""

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.errors import unauthorized
from hastori_api.ratelimit import LoginLimiter
from hastori_api.scope import SiteScope, load_scope
from hastori_api.security import decode_access_token
from hastori_api.tokens import RefreshStore
from hastori_common.settings import Settings

# auto_error=False: a missing header answers with this API's own error format.
bearer = HTTPBearer(auto_error=False, scheme_name="bearerAuth", description="Access token")


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def get_refresh_store(request: Request) -> RefreshStore:
    store: RefreshStore = request.app.state.refresh_store
    return store


def get_limiter(request: Request) -> LoginLimiter:
    limiter: LoginLimiter = request.app.state.limiter
    return limiter


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_scope(
    settings: SettingsDep,
    session: Session,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> SiteScope:
    if credentials is None:
        raise unauthorized()
    user_id = decode_access_token(settings, credentials.credentials)
    scope = await load_scope(session, user_id)
    if scope is None:  # a valid token for a user that no longer exists
        raise unauthorized("Invalid access token")
    return scope


Scope = Annotated[SiteScope, Depends(get_scope)]


def require_roles(*roles: str) -> Callable[[SiteScope], SiteScope]:
    """A dependency that lets only these roles through (403 otherwise)."""

    def check(scope: Scope) -> SiteScope:
        scope.require_role(*roles)
        return scope

    return check
