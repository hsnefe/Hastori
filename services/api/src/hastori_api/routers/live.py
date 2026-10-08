"""Live updates: a ticket for the WebSocket, and the WebSocket itself."""

import logging
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from redis.asyncio import Redis

from hastori_api.deps import Scope, SettingsDep, get_redis
from hastori_api.errors import COMMON_ERRORS, ApiError, ErrorResponse
from hastori_api.live import (
    CLOSE_INTERNAL,
    CLOSE_TRY_AGAIN,
    CLOSE_UNAUTHORIZED,
    Hub,
    serve_connection,
)
from hastori_api.schemas import WsTicketOut
from hastori_api.scope import load_scope
from hastori_api.tickets import consume_ticket, issue_ticket

log = logging.getLogger("api.live")

router = APIRouter(tags=["live"])

RedisDep = Annotated[Redis, Depends(get_redis)]


@router.post(
    "/ws-ticket",
    response_model=WsTicketOut,
    summary="Ticket for the WebSocket",
    description=(
        "Returns a single-use ticket, valid for 30 seconds. Open `GET /api/v1/ws?ticket=...` "
        "(a WebSocket) with it. The ticket, not the access token, goes into the URL, so the token "
        "never lands in a log or in the browser history. A connection lives at most 15 minutes; "
        "ask for a new ticket and reconnect."
    ),
    responses={
        401: COMMON_ERRORS[401],
        429: {"model": ErrorResponse, "description": "Too many tickets"},
    },
)
async def ws_ticket(scope: Scope, settings: SettingsDep, redis: RedisDep) -> WsTicketOut:
    # A signed-in user could otherwise mint tickets (and sockets) without limit.
    window = int(time.time() // 60)
    key = f"hastori:wsticket-rate:{scope.user_id}:{window}"
    issued = await redis.incr(key)
    if issued == 1:
        await redis.expire(key, 120)
    if issued > settings.ws_tickets_per_minute:
        raise ApiError(429, "Too many WebSocket tickets", headers={"Retry-After": "30"})
    ticket = await issue_ticket(redis, scope.user_id, settings.ws_ticket_ttl_s)
    return WsTicketOut(ticket=ticket, expires_in=settings.ws_ticket_ttl_s)


@router.websocket("/ws")
async def live(websocket: WebSocket) -> None:
    """Live measurements and alarm events of one site at a time.

    Client to server: `{"type": "subscribe", "site_id": "..."}` and `{"type": "unsubscribe"}`.
    Server to client: `hello`, `measurement`, `alarm.opened`, `alarm.acknowledged`,
    `alarm.cleared`, `resync` (fetch the current state over REST), `hb`, `error`.
    Close codes: 4401 no valid ticket, 4403 site outside your scope, 4408 maximum age reached
    (reconnect with a new ticket), 1013 too slow.
    """
    app = websocket.app
    settings = app.state.settings
    origin = websocket.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in settings.ws_origins:
        await websocket.close(code=1008)  # before accept: the handshake is answered with 403
        return
    await websocket.accept()
    try:
        user_id = await consume_ticket(app.state.redis, websocket.query_params.get("ticket", ""))
    except Exception as exc:
        log.warning("ticket lookup failed", extra={"error": str(exc)})
        await websocket.close(code=CLOSE_TRY_AGAIN)
        return
    if user_id is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    try:
        async with app.state.sessionmaker() as session:
            scope = await load_scope(session, user_id)
    except Exception as exc:
        log.warning("scope lookup failed", extra={"error": str(exc)})
        await websocket.close(code=CLOSE_INTERNAL)
        return
    if scope is None:  # the user was removed after the ticket was issued
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    hub: Hub = app.state.hub
    open_by_user: dict[uuid.UUID, int] = app.state.ws_open
    if (
        open_by_user.get(user_id, 0) >= settings.ws_max_per_user
        or sum(open_by_user.values()) >= settings.ws_max_total
    ):
        await websocket.close(code=CLOSE_TRY_AGAIN)
        return
    open_by_user[user_id] = open_by_user.get(user_id, 0) + 1
    try:
        await serve_connection(
            websocket,
            hub,
            scope,
            queue_size=settings.ws_queue_size,
            heartbeat_s=settings.ws_heartbeat_s,
            max_age_s=settings.ws_max_age_s,
        )
    finally:
        open_by_user[user_id] -= 1
        if open_by_user[user_id] <= 0:
            del open_by_user[user_id]
