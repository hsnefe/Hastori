"""Liveness and readiness (outside /api/v1, no authentication)."""

import asyncio

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness: database and Redis reachable")
async def readyz(request: Request, response: Response) -> dict[str, bool]:
    status = {"db": False, "redis": False}
    try:
        async with request.app.state.engine.connect() as conn:
            await asyncio.wait_for(conn.execute(text("SELECT 1")), 3)
        status["db"] = True
    except Exception:
        pass
    try:
        await asyncio.wait_for(request.app.state.redis.ping(), 3)
        status["redis"] = True
    except Exception:
        pass
    if not all(status.values()):
        response.status_code = 503
    return status
