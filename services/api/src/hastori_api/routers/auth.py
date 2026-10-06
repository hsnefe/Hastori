"""Sign in, refresh, sign out."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import func, select

from hastori_api.deps import (
    Scope,
    Session,
    SettingsDep,
    get_limiter,
    get_refresh_store,
)
from hastori_api.errors import COMMON_ERRORS, ApiError, ErrorResponse
from hastori_api.queries.sites import list_sites
from hastori_api.ratelimit import LoginLimiter
from hastori_api.schemas import LoginIn, MeOut, SiteOut, TokenOut
from hastori_api.scope import load_scope
from hastori_api.security import issue_access_token, verify_password
from hastori_api.tokens import RefreshStore
from hastori_common.models import User
from hastori_common.settings import Settings

log = logging.getLogger("api.auth")

COOKIE = "refresh_token"
COOKIE_PATH = "/api/v1/auth"

router = APIRouter(prefix="/auth", tags=["auth"])

Limiter = Annotated[LoginLimiter, Depends(get_limiter)]
Store = Annotated[RefreshStore, Depends(get_refresh_store)]


def _set_cookie(response: Response, settings: Settings, token: str) -> None:
    # httpOnly: JavaScript (and so an XSS) never sees it. SameSite=Strict + a path of its own:
    # it is sent only by this site, only to the auth endpoints.
    response.set_cookie(
        COOKIE,
        token,
        max_age=settings.refresh_token_ttl_s,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        path=COOKIE_PATH,
    )


def _cleared_cookie_header(settings: Settings) -> dict[str, str]:
    """For error responses (a raised error carries headers, not the injected Response)."""
    secure = "; Secure" if settings.cookie_secure else ""
    return {
        "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path={COOKIE_PATH}; HttpOnly; SameSite=Strict{secure}"
    }


def _token_out(settings: Settings, user_id: uuid.UUID) -> TokenOut:
    return TokenOut(
        access_token=issue_access_token(settings, user_id), expires_in=settings.access_token_ttl_s
    )


@router.post(
    "/login",
    response_model=TokenOut,
    summary="Sign in",
    description=(
        "Returns a short-lived access token (send it as `Authorization: Bearer ...`) and sets a "
        "refresh token in an httpOnly cookie. After 5 failed attempts for the same e-mail and "
        "address, further attempts get 429 for 5 minutes."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Wrong e-mail or password"},
        422: COMMON_ERRORS[422],
        429: {"model": ErrorResponse, "description": "Too many failed attempts (see Retry-After)"},
    },
)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    session: Session,
    settings: SettingsDep,
    limiter: Limiter,
    store: Store,
) -> TokenOut:
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "unknown"
    wait = await limiter.blocked_for(email, ip)
    if wait:
        raise ApiError(429, "Too many failed sign-in attempts", headers={"Retry-After": str(wait)})
    row = (
        await session.execute(
            select(User.id, User.password_hash).where(func.lower(User.email) == email)
        )
    ).first()
    ok = await verify_password(body.password, row.password_hash if row else None)
    if row is None or not ok:
        await limiter.failed(email, ip)
        # The same answer for an unknown address and a wrong password.
        raise ApiError(401, "Wrong e-mail or password")
    await limiter.succeeded(email, ip)
    _set_cookie(response, settings, await store.issue(row.id))
    return _token_out(settings, row.id)


@router.post(
    "/refresh",
    response_model=TokenOut,
    summary="New access token",
    description=(
        "Uses the refresh cookie and rotates it: the response sets a new one. An old refresh "
        "token still works for 20 seconds (parallel tabs); after that its use revokes the whole "
        "session."
    ),
    responses={401: {"model": ErrorResponse, "description": "No, unknown or reused refresh token"}},
)
async def refresh(
    response: Response,
    session: Session,
    settings: SettingsDep,
    store: Store,
    refresh_token: Annotated[str | None, Cookie(alias=COOKIE)] = None,
) -> TokenOut:
    gone = _cleared_cookie_header(settings)
    if not refresh_token:
        raise ApiError(401, "No refresh token", headers=gone)
    rotation = await store.rotate(refresh_token)
    if rotation.status == "reused":
        log.warning(
            "refresh token reuse: session revoked", extra={"user_id": str(rotation.user_id)}
        )
        raise ApiError(401, "Refresh token was already used; sign in again", headers=gone)
    if rotation.status != "ok" or rotation.token is None or rotation.user_id is None:
        raise ApiError(401, "Invalid refresh token", headers=gone)
    if await load_scope(session, rotation.user_id) is None:  # the user was removed meanwhile
        await store.revoke(rotation.token)
        raise ApiError(401, "Invalid refresh token", headers=gone)
    _set_cookie(response, settings, rotation.token)
    return _token_out(settings, rotation.user_id)


@router.post(
    "/logout",
    status_code=204,
    summary="Sign out",
    description="Ends the whole session the refresh cookie belongs to. Needs no access token.",
)
async def logout(
    response: Response,
    settings: SettingsDep,
    store: Store,
    refresh_token: Annotated[str | None, Cookie(alias=COOKIE)] = None,
) -> Response:
    if refresh_token:
        await store.revoke(refresh_token)
    out = Response(status_code=204)
    out.headers.update(_cleared_cookie_header(settings))
    return out


@router.get(
    "/me",
    response_model=MeOut,
    summary="Who am I",
    responses={401: COMMON_ERRORS[401]},
)
async def me(scope: Scope, session: Session) -> MeOut:
    sites = await list_sites(session, scope)
    return MeOut(
        id=scope.user_id,
        email=scope.email,
        role=scope.role,
        org_id=scope.org_id,
        sites=[SiteOut.model_validate(s) for s in sites],
    )
