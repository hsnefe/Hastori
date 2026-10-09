"""Sign in, refresh, sign out."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import func, select, update

from hastori_api.audit import audit
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
from hastori_api.schemas import (
    DemoConfigOut,
    DemoLoginIn,
    LoginIn,
    MeOut,
    PasswordChangeIn,
    SiteOut,
    TokenOut,
)
from hastori_api.scope import load_scope
from hastori_api.security import hash_password_async, issue_access_token, verify_password
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


def _cookie_header(settings: Settings, token: str) -> dict[str, str]:
    """The refresh cookie as a header, for an error response that must still deliver it."""
    secure = "; Secure" if settings.cookie_secure else ""
    return {
        "Set-Cookie": (
            f"{COOKIE}={token}; Max-Age={settings.refresh_token_ttl_s}; Path={COOKIE_PATH}; "
            f"HttpOnly; SameSite=Strict{secure}"
        )
    }


def _token_out(settings: Settings, user_id: uuid.UUID, token_version: int = 0) -> TokenOut:
    return TokenOut(
        access_token=issue_access_token(settings, user_id, token_version=token_version),
        expires_in=settings.access_token_ttl_s,
    )


@router.post(
    "/login",
    response_model=TokenOut,
    summary="Sign in",
    description=(
        "Returns a short-lived access token (send it as `Authorization: Bearer ...`) and sets a "
        "refresh token in an httpOnly cookie. After 5 failed attempts for the same e-mail and "
        "address, further attempts get 429 for 5 minutes (so do 30 failures from one address, "
        "whatever the e-mail)."
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
            select(User.id, User.password_hash, User.is_active, User.token_version).where(
                func.lower(User.email) == email
            )
        )
    ).first()
    # Hashing takes ~100 ms of CPU and may queue: do not hold a pooled connection meanwhile
    # (ten simultaneous sign-ins would otherwise empty the pool for everybody else).
    await session.close()
    ok = await verify_password(body.password, row.password_hash if row else None)
    if row is None or not ok or not row.is_active:
        await limiter.failed(email, ip)
        # The same answer for an unknown address, a wrong password and a deactivated account.
        raise ApiError(401, "Wrong e-mail or password")
    await limiter.succeeded(email, ip)
    _set_cookie(response, settings, await store.issue(row.id, row.token_version))
    return _token_out(settings, row.id, row.token_version)


@router.get(
    "/demo",
    response_model=DemoConfigOut,
    summary="Is the passwordless demo sign-in on",
    description="Lets the sign-in page decide whether to offer the demo buttons.",
)
async def demo_config(settings: SettingsDep) -> DemoConfigOut:
    return DemoConfigOut(enabled=settings.demo_login)


@router.post(
    "/demo",
    response_model=TokenOut,
    summary="Sign in as the demo viewer or the demo site admin, without a password",
    description=(
        "Only when the operator set `DEMO_LOGIN=true` (404 otherwise). Signs in as the one fixed "
        "viewer or the one fixed site admin; the system admin cannot be reached this way. Same "
        "answer as `/auth/login`: an access token and the refresh cookie."
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Demo sign-in is off"},
        422: COMMON_ERRORS[422],
    },
)
async def demo_login(
    body: DemoLoginIn,
    response: Response,
    session: Session,
    settings: SettingsDep,
    store: Store,
) -> TokenOut:
    if not settings.demo_login:
        raise ApiError(404, "Not found")
    email = settings.demo_viewer_email if body.role == "viewer" else settings.demo_site_admin_email
    row = (
        await session.execute(
            select(User.id, User.role, User.is_active, User.token_version).where(
                func.lower(User.email) == email.lower()
            )
        )
    ).first()
    # The account must exist and really have the role asked for: a wrong DEMO_*_EMAIL (say, the
    # system admin's) must never turn this into a back door.
    if row is None or row.role != body.role or not row.is_active:
        raise ApiError(404, "Not found")
    await session.close()
    _set_cookie(response, settings, await store.issue(row.id, row.token_version))
    return _token_out(settings, row.id, row.token_version)


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
    try:
        scope = await load_scope(session, rotation.user_id)
    except Exception as exc:
        # The token is already rotated. If the answer were a bare 503 the new cookie would never
        # reach the browser, and after the grace window the old one would count as stolen and
        # revoke the whole session. Deliver the cookie with the error; the client retries.
        log.warning("refresh: user lookup failed", extra={"error": str(exc)[:200]})
        raise ApiError(
            503,
            "A backing service is unavailable; try again shortly",
            headers={"Retry-After": "2", **_cookie_header(settings, rotation.token)},
        ) from exc
    # The user was removed or deactivated meanwhile, or changed their password (or had it
    # changed): the session was signed in under an older token version.
    if scope is None or scope.token_version != rotation.token_version:
        await store.revoke(rotation.token)
        raise ApiError(401, "Invalid refresh token", headers=gone)
    _set_cookie(response, settings, rotation.token)
    return _token_out(settings, rotation.user_id, scope.token_version)


@router.post(
    "/password",
    response_model=TokenOut,
    summary="Change your own password",
    description=(
        "Needs the current password. Every other session of the user ends at once (their access "
        "tokens too, within seconds of the change: the version check happens on every request); "
        "this one continues with the new token and cookie in the response."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Not signed in, or wrong current password"},
        422: COMMON_ERRORS[422],
    },
)
async def change_password(
    body: PasswordChangeIn,
    response: Response,
    scope: Scope,
    session: Session,
    settings: SettingsDep,
    store: Store,
    refresh_token: Annotated[str | None, Cookie(alias=COOKIE)] = None,
) -> TokenOut:
    current = await session.scalar(select(User.password_hash).where(User.id == scope.user_id))
    await session.close()
    if not await verify_password(body.current_password, current):
        # 401, not 403: the browser's sign-in screen is the only thing a wrong password can mean.
        raise ApiError(401, "Wrong current password")
    new_hash = await hash_password_async(body.new_password)
    version = await session.scalar(
        update(User)
        .where(User.id == scope.user_id)
        .values(password_hash=new_hash, token_version=User.token_version + 1)
        .returning(User.token_version)
    )
    audit(session, scope, "auth.password_change", "user", scope.user_id)
    await session.commit()
    if refresh_token:
        await store.revoke(refresh_token)
    assert version is not None
    _set_cookie(response, settings, await store.issue(scope.user_id, version))
    return _token_out(settings, scope.user_id, version)


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
