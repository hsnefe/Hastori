"""Live updates: the WebSocket hub.

One Redis pub/sub connection per API process (PSUBSCRIBE hastori:site:*) feeds every WebSocket
connection of that process. A connection subscribes to one site at a time, and only a site in the
user's scope (built from the database when the connection opened, see scope.py). So the hub never
decides who may see what: the handler checks the scope, the hub only routes by site.

Events are hints. Pub/sub can lose a message, a process can be restarted, so a client treats
`measurement` as "something to draw" and `alarm.*` as "ask REST again", and on every `resync` it
fetches the current state over REST. `resync` is sent right after a subscription (the client
fetches its snapshot only once the live stream is already registered, so nothing falls in the
gap) and to every connection when the Redis link comes back.

A slow client must not hold anybody else up: every connection has its own bounded queue and a
writer task; a full queue closes that connection (1013) instead of blocking the fan-out.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from hastori_api.scope import SiteScope
from hastori_common.events import CHANNEL_PREFIX

log = logging.getLogger("api.live")

PATTERN = f"{CHANNEL_PREFIX}*"
MAX_CLIENT_MESSAGE = 4096

# WebSocket close codes. 4xxx are ours.
CLOSE_TOO_BIG = 1009
CLOSE_TRY_AGAIN = 1013  # the client is too slow (or the server cannot serve right now)
CLOSE_INTERNAL = 1011
CLOSE_UNAUTHORIZED = 4401  # no ticket, an unknown or used one, or a user that is gone
CLOSE_FORBIDDEN = 4403  # a site outside the user's scope
CLOSE_MAX_AGE = 4408  # the connection reached its maximum age: reconnect with a new ticket

HEARTBEAT = json.dumps({"type": "hb"})
RESYNC = json.dumps({"type": "resync"})


def server_message(kind: str, **fields: Any) -> str:
    return json.dumps({"type": kind, **fields})


class Connection:
    """One WebSocket: the queue the writer drains, and the reason it is going away."""

    def __init__(self, scope: SiteScope, queue_size: int) -> None:
        self.scope = scope
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.site_id: uuid.UUID | None = None
        self.close_code: int | None = None
        self.closed = asyncio.Event()

    def close(self, code: int) -> None:
        """Ask for the connection to be closed with `code` (the first reason wins)."""
        if self.close_code is None:
            self.close_code = code
            self.closed.set()

    def offer(self, message: str) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.close(CLOSE_TRY_AGAIN)


class Hub:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._by_site: dict[uuid.UUID, set[Connection]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self.connected = False  # the pub/sub link is up

    # -- membership -------------------------------------------------------------------------
    def subscribe(self, conn: Connection, site_id: uuid.UUID) -> None:
        self.unsubscribe(conn)
        conn.site_id = site_id
        self._by_site[site_id].add(conn)

    def unsubscribe(self, conn: Connection) -> None:
        if conn.site_id is not None:
            members = self._by_site.get(conn.site_id)
            if members is not None:
                members.discard(conn)
                if not members:
                    del self._by_site[conn.site_id]
            conn.site_id = None

    def count(self) -> int:
        return sum(len(m) for m in self._by_site.values())

    # -- fan-out ----------------------------------------------------------------------------
    def dispatch(self, channel: str, data: str) -> None:
        try:
            site_id = uuid.UUID(channel.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return
        for conn in list(self._by_site.get(site_id, ())):
            conn.offer(data)

    def resync_all(self) -> None:
        for members in list(self._by_site.values()):
            for conn in list(members):
                conn.offer(RESYNC)

    # -- the Redis link ---------------------------------------------------------------------
    async def run(self, backoff_max_s: float = 10.0, backoff_s: float = 0.5) -> None:
        """Listen until cancelled; reconnect with a growing wait when the link breaks."""
        backoff, first = backoff_s, True
        while True:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.psubscribe(PATTERN)
                self.connected = True
                if not first:
                    log.warning("redis link is back: telling clients to resync")
                    self.resync_all()  # whatever was published meanwhile is lost
                first, backoff = False, backoff_s
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is None or message.get("type") != "pmessage":
                        continue
                    channel, data = message["channel"], message["data"]
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    if isinstance(data, bytes):
                        data = data.decode()
                    self.dispatch(channel, data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("redis link lost", extra={"error": str(exc), "retry_in_s": backoff})
            finally:
                self.connected = False
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(pubsub.aclose(), 2)  # type: ignore[no-untyped-call]
            await asyncio.sleep(backoff)
            backoff = min(backoff_max_s, backoff * 2)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="live-hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


async def serve_connection(
    websocket: WebSocket,
    hub: Hub,
    scope: SiteScope,
    *,
    queue_size: int,
    heartbeat_s: float,
    max_age_s: float,
) -> None:
    """Run an accepted, authenticated connection until it ends, then close it."""
    conn = Connection(scope, queue_size)
    conn.offer(
        server_message("hello", sites=sorted(str(s) for s in scope.site_ids), ts=time.time())
    )
    disconnected = False

    async def reader() -> None:
        nonlocal disconnected
        try:
            while True:
                text = await websocket.receive_text()
                if len(text) > MAX_CLIENT_MESSAGE:
                    conn.close(CLOSE_TOO_BIG)
                    return
                handle_client_message(hub, conn, text)
        except WebSocketDisconnect:
            disconnected = True

    async def writer() -> None:
        while True:
            await websocket.send_text(await conn.queue.get())

    async def ticker() -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_age_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                conn.close(CLOSE_MAX_AGE)
                return
            await asyncio.sleep(min(heartbeat_s, remaining))
            if loop.time() < deadline:
                conn.offer(HEARTBEAT)

    tasks = [
        asyncio.create_task(reader(), name="ws-reader"),
        asyncio.create_task(writer(), name="ws-writer"),
        asyncio.create_task(ticker(), name="ws-ticker"),
        asyncio.create_task(conn.closed.wait(), name="ws-closed"),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:  # a writer that failed means the socket is gone
            if not task.cancelled() and task.exception() is not None:
                disconnected = True
    finally:
        hub.unsubscribe(conn)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not disconnected:
            # A slow client may not even accept the close frame: give it a moment, then give up.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(websocket.close(conn.close_code or 1000), 5)


def handle_client_message(hub: Hub, conn: Connection, text: str) -> None:
    try:
        message = json.loads(text)
        kind = message["type"]
    except (ValueError, KeyError, TypeError, RecursionError):
        conn.offer(server_message("error", code="bad_message", message="Expected a JSON object"))
        return
    if kind == "subscribe":
        try:
            site_id = uuid.UUID(str(message.get("site_id")))
        except ValueError:
            conn.offer(server_message("error", code="bad_message", message="Invalid site_id"))
            return
        if site_id not in conn.scope.site_ids:
            # Same answer as REST gives for a site outside the scope, but a socket has no 404.
            conn.close(CLOSE_FORBIDDEN)
            return
        hub.subscribe(conn, site_id)
        conn.offer(RESYNC)
    elif kind == "unsubscribe":
        hub.unsubscribe(conn)
    else:
        conn.offer(server_message("error", code="bad_message", message="Unknown message type"))
