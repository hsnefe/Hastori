"""The WebSocket: tickets, the scope of a subscription, fan-out, slow clients, a lost Redis link,
connection age. The application runs in process; a small ASGI driver plays the browser (httpx's
test transport does not speak WebSocket). What only a real server shows - uvicorn closing sockets
on SIGTERM, a proxy in front - is checked by `make e2e`."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from conftest import ApiHarness
from hastori_api.live import (
    CLOSE_FORBIDDEN,
    CLOSE_MAX_AGE,
    CLOSE_TOO_BIG,
    CLOSE_TRY_AGAIN,
    CLOSE_UNAUTHORIZED,
    Connection,
    Hub,
)
from hastori_api.scope import SiteScope
from hastori_api.tickets import PREFIX
from hastori_common.events import channel
from hastori_common.seed_data import load_seed

SEED = load_seed()
IZMIR = SEED.site_by_key("izmir").id
ANTALYA = SEED.site_by_key("antalya").id
ORIGIN = "http://127.0.0.1:8080"


class Socket:
    """A WebSocket client speaking ASGI directly to the application."""

    def __init__(self, app: Any, ticket: str | None, origin: str | None = ORIGIN) -> None:
        query = b"" if ticket is None else f"ticket={ticket}".encode()
        headers = [(b"host", b"test")]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/api/v1/ws",
            "raw_path": b"/api/v1/ws",
            "root_path": "",
            "query_string": query,
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
            "subprotocols": [],
        }
        self.app = app
        self._in: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.accepted = False
        self.close_code: int | None = None

    async def open(self) -> "Socket":
        await self._in.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(self.scope, self._in.get, self._out.put))
        first = await asyncio.wait_for(self._out.get(), 5)
        if first["type"] == "websocket.accept":
            self.accepted = True
        else:  # closed before accept: the handshake is answered with HTTP 403
            self.close_code = first.get("code")
        return self

    async def send(self, message: dict[str, Any] | str) -> None:
        text = message if isinstance(message, str) else json.dumps(message)
        await self._in.put({"type": "websocket.receive", "text": text})

    async def next(self, within: float = 2.0) -> dict[str, Any] | None:
        """The next message from the server; None once it closed (`close_code` is then set)."""
        if self.close_code is not None:
            return None
        frame = await asyncio.wait_for(self._out.get(), within)
        if frame["type"] == "websocket.close":
            self.close_code = frame.get("code", 1000)
            return None
        assert frame["type"] == "websocket.send", frame
        message: dict[str, Any] = json.loads(frame["text"])
        return message

    async def quiet(self, seconds: float = 0.3) -> bool:
        """True if nothing arrives for a while."""
        try:
            await asyncio.wait_for(self._out.get(), seconds)
        except TimeoutError:
            return True
        return False

    async def drain(self) -> list[dict[str, Any]]:
        out = []
        while (m := await self._next_or_none(0.2)) is not None:
            out.append(m)
        return out

    async def _next_or_none(self, within: float) -> dict[str, Any] | None:
        try:
            return await self.next(within)
        except TimeoutError:
            return None

    async def leave(self) -> None:
        await self._in.put({"type": "websocket.disconnect", "code": 1000})
        if self.task is not None:
            await asyncio.wait_for(self.task, 5)


@pytest.fixture
async def live(api: ApiHarness) -> AsyncIterator[ApiHarness]:
    hub: Hub = api.app.state.hub
    hub.start()
    for _ in range(100):  # the pattern subscription must be in place before anything is published
        if hub.connected:
            break
        await asyncio.sleep(0.02)
    assert hub.connected
    try:
        yield api
    finally:
        await hub.stop()


async def ticket_for(api: ApiHarness, who: str) -> str:
    client = await api.signed_in(who)
    r = await client.post("/ws-ticket")
    assert r.status_code == 200, r.text
    assert r.json()["expires_in"] == 30
    return str(r.json()["ticket"])


async def connect(api: ApiHarness, who: str, origin: str | None = ORIGIN) -> Socket:
    sock = await Socket(api.app, await ticket_for(api, who), origin).open()
    assert sock.accepted
    hello = await sock.next()
    assert hello is not None and hello["type"] == "hello"
    return sock


async def subscribe(sock: Socket, site: uuid.UUID) -> None:
    await sock.send({"type": "subscribe", "site_id": str(site)})
    assert await sock.next() == {"type": "resync"}


async def publish(api: ApiHarness, site: uuid.UUID, event: dict[str, Any]) -> None:
    await api.redis.publish(channel(site), json.dumps(event))


# -- tickets ------------------------------------------------------------------------------------


async def test_a_ticket_opens_one_connection_only(live: ApiHarness) -> None:
    ticket = await ticket_for(live, "izmir_viewer")
    first = await Socket(live.app, ticket).open()
    assert first.accepted and (await first.next() or {}).get("type") == "hello"
    second = await Socket(live.app, ticket).open()
    assert second.accepted and await second.next() is None
    assert second.close_code == CLOSE_UNAUTHORIZED
    await first.leave()


async def test_the_ticket_is_not_stored_in_the_clear(live: ApiHarness) -> None:
    ticket = await ticket_for(live, "izmir_viewer")
    keys = [k async for k in live.redis.scan_iter(f"{PREFIX}*")]
    assert len(keys) == 1 and ticket not in keys[0]
    assert 0 < await live.redis.ttl(keys[0]) <= 30


async def test_an_expired_unknown_or_missing_ticket_is_refused(live: ApiHarness) -> None:
    ticket = await ticket_for(live, "izmir_viewer")
    (key,) = [k async for k in live.redis.scan_iter(f"{PREFIX}*")]
    await live.redis.pexpire(key, 1)
    await asyncio.sleep(0.05)
    for bad in (ticket, "not-a-ticket", None):
        sock = await Socket(live.app, bad).open()
        assert sock.accepted and await sock.next() is None
        assert sock.close_code == CLOSE_UNAUTHORIZED, bad


async def test_the_ticket_of_a_user_who_is_gone_is_refused(live: ApiHarness) -> None:
    ticket = await ticket_for(live, "izmir_viewer")
    await live.redis.set(
        (await anext(live.redis.scan_iter(f"{PREFIX}*"))), str(uuid.uuid4())
    )  # the user does not exist (any more)
    sock = await Socket(live.app, ticket).open()
    assert await sock.next() is None and sock.close_code == CLOSE_UNAUTHORIZED


async def test_a_ticket_needs_a_signed_in_user(live: ApiHarness) -> None:
    assert (await live.client().post("/ws-ticket")).status_code == 401


async def test_a_browser_origin_that_is_not_listed_is_refused(live: ApiHarness) -> None:
    ticket = await ticket_for(live, "izmir_viewer")
    sock = await Socket(live.app, ticket, "https://evil.example").open()
    assert not sock.accepted  # the handshake gets 403
    assert (await live.redis.scan(match=f"{PREFIX}*"))[1], "the ticket was not even looked at"
    for ok in ("http://localhost:3000", "http://127.0.0.1:8080/", None):  # None: not a browser
        sock = await Socket(live.app, await ticket_for(live, "izmir_viewer"), ok).open()
        assert sock.accepted, ok


# -- subscriptions ------------------------------------------------------------------------------


async def test_a_subscription_matrix(live: ApiHarness) -> None:
    """Who may subscribe to which site; a refusal closes the socket with 4403."""
    expected = {
        "izmir_viewer": {IZMIR: True, ANTALYA: False},
        "izmir_admin": {IZMIR: True, ANTALYA: False},
        "antalya_viewer": {IZMIR: False, ANTALYA: True},
        "antalya_admin": {IZMIR: False, ANTALYA: True},
        "admin": {IZMIR: True, ANTALYA: True},
    }
    for who, sites in expected.items():
        for site, allowed in sites.items():
            sock = await connect(live, who)
            await sock.send({"type": "subscribe", "site_id": str(site)})
            if allowed:
                assert await sock.next() == {"type": "resync"}, (who, site)
                await sock.leave()
            else:
                assert await sock.next() is None, (who, site)
                assert sock.close_code == CLOSE_FORBIDDEN, (who, site)


async def test_hello_lists_the_sites_of_the_user(live: ApiHarness) -> None:
    sock = await Socket(live.app, await ticket_for(live, "izmir_viewer")).open()
    hello = await sock.next()
    assert hello is not None and hello["sites"] == [str(IZMIR)]


async def test_events_reach_only_the_subscribers_of_that_site(live: ApiHarness) -> None:
    izmir = await connect(live, "izmir_viewer")
    antalya = await connect(live, "antalya_viewer")
    idle = await connect(live, "admin")  # a system admin who has not subscribed to anything
    await subscribe(izmir, IZMIR)
    await subscribe(antalya, ANTALYA)

    event = {"type": "measurement", "device_id": str(uuid.uuid4()), "ts": 1.0, "metrics": {"a": 1}}
    await publish(live, IZMIR, event)
    assert await izmir.next() == event
    assert await antalya.quiet() and await idle.quiet()

    alarm = {"type": "alarm.opened", "alarm_id": str(uuid.uuid4()), "site_id": str(ANTALYA)}
    await publish(live, ANTALYA, alarm)
    assert await antalya.next() == alarm
    assert await izmir.quiet()


async def test_subscribing_again_moves_the_connection_and_unsubscribe_stops_events(
    live: ApiHarness,
) -> None:
    sock = await connect(live, "admin")
    await subscribe(sock, IZMIR)
    await subscribe(sock, ANTALYA)  # one site at a time
    await publish(live, IZMIR, {"type": "measurement", "ts": 1})
    assert await sock.quiet()
    await publish(live, ANTALYA, {"type": "measurement", "ts": 2})
    assert (await sock.next() or {})["ts"] == 2
    await sock.send({"type": "unsubscribe"})
    await asyncio.sleep(0.05)
    await publish(live, ANTALYA, {"type": "measurement", "ts": 3})
    assert await sock.quiet()


async def test_a_departed_client_leaves_no_trace_in_the_hub(live: ApiHarness) -> None:
    hub: Hub = live.app.state.hub
    sock = await connect(live, "izmir_viewer")
    await subscribe(sock, IZMIR)
    assert hub.count() == 1
    await sock.leave()
    assert hub.count() == 0


async def test_bad_messages_get_an_error_and_the_connection_stays(live: ApiHarness) -> None:
    sock = await connect(live, "izmir_viewer")
    for bad in ("not json", "[]", '{"type": "subscribe", "site_id": "nope"}', '{"type": "fly"}'):
        await sock.send(bad)
        reply = await sock.next()
        assert reply is not None and reply["type"] == "error" and reply["code"] == "bad_message"
    await subscribe(sock, IZMIR)  # still works


async def test_an_oversized_message_closes_the_connection(live: ApiHarness) -> None:
    sock = await connect(live, "izmir_viewer")
    await sock.send("x" * 5000)
    assert await sock.next() is None and sock.close_code == CLOSE_TOO_BIG


# -- slow clients and a lost Redis link ---------------------------------------------------------


def scope_of(*sites: uuid.UUID) -> SiteScope:
    return SiteScope(uuid.uuid4(), uuid.uuid4(), "u@example.test", "viewer", frozenset(sites))


async def test_a_slow_client_is_closed_and_the_others_are_not_held_up() -> None:
    hub = Hub(redis=None)  # type: ignore[arg-type]
    slow, fast = Connection(scope_of(IZMIR), 3), Connection(scope_of(IZMIR), 256)
    hub.subscribe(slow, IZMIR)
    hub.subscribe(fast, IZMIR)
    for i in range(10):  # nobody drains `slow`
        hub.dispatch(channel(IZMIR), json.dumps({"type": "measurement", "ts": i}))
    assert slow.close_code == CLOSE_TRY_AGAIN and slow.closed.is_set()
    assert fast.close_code is None and fast.queue.qsize() == 10
    assert slow.queue.qsize() == 3  # the queue is bounded: memory does not grow


async def test_a_slow_socket_is_closed_with_1013_end_to_end(live: ApiHarness) -> None:
    live.app.state.settings.ws_queue_size = 5
    sock = await connect(live, "izmir_viewer")
    await subscribe(sock, IZMIR)
    hub: Hub = live.app.state.hub
    for i in range(50):  # a burst the reader below cannot keep up with: it does not read at all
        hub.dispatch(channel(IZMIR), json.dumps({"type": "measurement", "ts": i}))
    seen = []
    while (m := await sock.next()) is not None:
        seen.append(m)
    assert sock.close_code == CLOSE_TRY_AGAIN and len(seen) <= 6
    live.app.state.settings.ws_queue_size = 256


class Flaky:
    """A Redis client whose first pub/sub dies after it subscribed; later ones are real."""

    def __init__(self, real: Any) -> None:
        self.real, self.calls = real, 0

    def pubsub(self) -> Any:
        self.calls += 1
        pubsub = self.real.pubsub()
        if self.calls == 1:

            async def broken(*_: Any, **__: Any) -> None:
                raise ConnectionError("link lost")

            pubsub.get_message = broken
        return pubsub


async def test_when_the_redis_link_comes_back_every_client_is_told_to_resync(
    api: ApiHarness,
) -> None:
    hub = Hub(Flaky(api.redis))  # type: ignore[arg-type]
    conn = Connection(scope_of(IZMIR), 256)
    hub.subscribe(conn, IZMIR)
    task = asyncio.create_task(hub.run(backoff_max_s=0.05, backoff_s=0.01))
    try:
        resync = await asyncio.wait_for(conn.queue.get(), 5)
        assert json.loads(resync) == {"type": "resync"}
        await asyncio.sleep(0.05)
        await api.redis.publish(channel(IZMIR), json.dumps({"type": "measurement", "ts": 1}))
        assert json.loads(await asyncio.wait_for(conn.queue.get(), 5))["ts"] == 1
        assert conn.queue.empty()  # exactly one resync: the first connection does not send one
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# -- heartbeat and age --------------------------------------------------------------------------


async def test_the_server_sends_heartbeats_and_closes_a_connection_at_its_maximum_age(
    live: ApiHarness,
) -> None:
    settings = live.app.state.settings
    settings.ws_heartbeat_s, settings.ws_max_age_s = 0.1, 0.45
    try:
        sock = await connect(live, "izmir_viewer")
        beats = 0
        while (m := await sock.next()) is not None:
            beats += m["type"] == "hb"
        assert beats >= 3 and sock.close_code == CLOSE_MAX_AGE
    finally:
        settings.ws_heartbeat_s, settings.ws_max_age_s = 25.0, 900.0
