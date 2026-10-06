"""The FastAPI application."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from hastori_api.errors import install_error_handlers
from hastori_api.ratelimit import LoginLimiter
from hastori_api.routers import alarm_rules, alarms, auth, devices, health, sites, users
from hastori_api.tokens import RefreshStore
from hastori_common.settings import Settings

DESCRIPTION = """
REST API of the Hastori telemetry platform.

**Sign in** with `POST /auth/login`, then press *Authorize* and paste the `access_token`.
Access tokens last 15 minutes; the refresh cookie renews them (`POST /auth/refresh`).

**Sites.** Every user sees only the sites assigned to them (a system admin sees all sites of the
organisation). An object of another site answers `404`, a role that may not do something `403`.

Errors always look like `{"error": {"code": "...", "message": "..."}}`.
"""

TAGS = [
    {"name": "auth", "description": "Sign in, refresh, sign out"},
    {"name": "sites", "description": "Sites and their devices"},
    {"name": "measurements", "description": "Time series and daily consumption"},
    {"name": "alarms", "description": "Alarm history, acknowledgement"},
    {"name": "alarm-rules", "description": "Alarm rule management (admins)"},
    {"name": "users", "description": "User management (system admin)"},
    {"name": "health", "description": "Liveness and readiness"},
]


def create_app(
    settings: Settings,
    engine: AsyncEngine,
    redis: Redis,
    *,
    owns_resources: bool = False,
) -> FastAPI:
    """`owns_resources`: close the engine and Redis client when the application stops."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_resources:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Hastori API",
        version="0.2.0",
        description=DESCRIPTION,
        openapi_tags=TAGS,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        redoc_url=None,
        swagger_ui_parameters={"persistAuthorization": True},
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis = redis
    app.state.limiter = LoginLimiter(redis)
    app.state.refresh_store = RefreshStore(
        redis, ttl_s=settings.refresh_token_ttl_s, grace_s=settings.refresh_grace_s
    )
    install_error_handlers(app)

    v1 = APIRouter(prefix="/api/v1")
    for router in (
        auth.router,
        sites.router,
        devices.router,
        alarms.router,
        alarm_rules.router,
        users.router,
    ):
        v1.include_router(router)
    app.include_router(v1)
    app.include_router(health.router)
    return app
