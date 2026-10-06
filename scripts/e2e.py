"""Day-2 end-to-end checks against the running stack. Exit code 1 if any check fails.

    make up && make seed && make simulate && make e2e

What it proves, on the real broker, database and Redis (the unit and integration tests prove the
pieces; this proves they are wired together):

- every service is healthy; the alarm queue has exactly one consumer and no backlog
- the authorization matrix (scripts/api_matrix.py): every endpoint x every demo user
- sign-in limits, refresh rotation with parallel tabs, replay of an old refresh token
- the demo scenario: an overheat opens exactly one critical alarm 30 s after the temperature
  crosses 80 C, a site admin acknowledges it, it closes by itself, and the Redis events arrive
  in that order; a spike opens nothing
- a restart of the alarm service in the middle of the 30 s count opens the alarm once, on time
- disabling the rule closes its open alarm within seconds (the rule change reaches the service)
- a malformed message lands in the dead-letter queue and the service stays up
- a compensation failure on the main panel raises the reactive-ratio warning

Writes: it injects faults into izmir-komp-1 and izmir-pano, acknowledges and disables/re-enables
the İzmir temperature rule. It restores what it changed (also when a check fails). Run it only
against a demo stack. `--skip-reactive` leaves out the slowest check (about 4 minutes);
`--skip-restart` leaves out the restart of the alarm service.
"""

import asyncio
import json
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from redis.asyncio import Redis

from hastori_common.events import channel
from hastori_common.seed_data import load_seed
from hastori_common.settings import ROOT, get_settings

# the sibling script, however this file is started or imported
sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_matrix import ACTORS, discover, run  # noqa: E402

SEED = load_seed()
IZMIR = SEED.site_by_key("izmir")
KOMP1 = SEED.device_by_key("izmir-komp-1")
PANO = SEED.device_by_key("izmir-pano")
TEMP_RULE = next(r for r in SEED.alarm_rules if r.device == "izmir-komp-1")
RATIO_RULE = next(r for r in SEED.alarm_rules if r.device == "izmir-pano")

API = "http://127.0.0.1:8000/api/v1"
ALARM_HTTP = "http://127.0.0.1:8003"
RABBIT = "http://127.0.0.1:15672"
RESULTS: list[tuple[str, bool, str]] = []
# Alarm opens when the count of 30 s is complete; samples come every 2 s, so up to one sample late.
OPEN_TOLERANCE_S = (30.0, 33.0)


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)


# -- pure helpers (unit-tested in tests/test_e2e_helpers.py) ---------------------------------


def histogram_buckets(text: str, name: str) -> dict[float, float]:
    """`name_bucket{le="2.0"} 17.0` lines of a Prometheus exposition -> {2.0: 17.0, inf: ...}."""
    out: dict[float, float] = {}
    for m in re.finditer(
        rf'^{re.escape(name)}_bucket\{{le="([^"]+)"\}}\s+([0-9.e+-]+)$', text, re.M
    ):
        out[float("inf") if m.group(1) == "+Inf" else float(m.group(1))] = float(m.group(2))
    return out


def share_within(
    before: dict[float, float], after: dict[float, float], limit: float
) -> float | None:
    """Share of the observations between two scrapes that were at most `limit` seconds, or None
    if there were none."""
    total = after.get(float("inf"), 0.0) - before.get(float("inf"), 0.0)
    if total <= 0:
        return None
    edges = [le for le in after if le <= limit]
    if not edges:
        return 0.0
    le = max(edges)
    return (after[le] - before.get(le, 0.0)) / total


def first_above(points: list[dict[str, Any]], threshold: float) -> datetime | None:
    """Time of the first point with a value above the threshold."""
    for p in points:
        if p["value"] > threshold:
            return parse_time(p["time"])
    return None


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def event_types(events: list[dict[str, Any]], alarm_id: str | None = None) -> list[str]:
    return [e["type"] for e in events if alarm_id is None or e["alarm_id"] == alarm_id]


# -- plumbing -------------------------------------------------------------------------------


async def wait_for(probe: Callable[[], Awaitable[Any]], seconds: float, every: float = 2.0) -> Any:
    """The first truthy result of `probe`, polled until `seconds` have passed."""
    deadline = time.monotonic() + seconds
    while True:
        result = await probe()
        if result:
            return result
        if time.monotonic() > deadline:
            return None
        await asyncio.sleep(every)


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "--profile", "sim", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=180,
    )


class Stack:
    def __init__(self) -> None:
        self.settings = get_settings()
        user, password = self._rabbit_credentials()
        self.rabbit = httpx.AsyncClient(base_url=RABBIT, auth=(user, password), timeout=15)
        self.plain = httpx.AsyncClient(base_url=API, timeout=15)
        self.sim = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.settings.sim_control_port}",
            headers={"Authorization": f"Bearer {self.settings.sim_control_token}"},
            timeout=10,
        )
        self.clients: dict[str, httpx.AsyncClient] = {}
        self.events: list[dict[str, Any]] = []
        self._listener: asyncio.Task[None] | None = None
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)

    def _rabbit_credentials(self) -> tuple[str, str]:
        m = re.match(r"amqp://([^:]+):([^@]+)@", self.settings.rabbitmq_url)
        return (m.group(1), m.group(2)) if m else ("hastori", "hastori_demo")

    # -- users ----------------------------------------------------------------------------
    def credentials(self, actor: str) -> tuple[str, str]:
        s = self.settings
        email = {
            "admin": "admin@demo.hastori.local",
            "izmir_admin": "izmir.admin@demo.hastori.local",
            "izmir_viewer": "izmir.izleyici@demo.hastori.local",
            "antalya_admin": "antalya.admin@demo.hastori.local",
            "antalya_viewer": "antalya.izleyici@demo.hastori.local",
        }[actor]
        password = {
            "admin": s.seed_system_admin_password,
            "izmir_admin": s.seed_site_admin_password,
            "antalya_admin": s.seed_site_admin_password,
            "izmir_viewer": s.seed_viewer_password,
            "antalya_viewer": s.seed_viewer_password,
        }[actor]
        return email, password

    async def sign_in(self, actor: str) -> httpx.AsyncClient:
        email, password = self.credentials(actor)
        client = httpx.AsyncClient(base_url=API, timeout=15)
        r = await client.post("/auth/login", json={"email": email, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"cannot sign in as {actor}: {r.status_code} {r.text[:120]}")
        client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        return client

    # -- events ---------------------------------------------------------------------------
    async def start_events(self) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(channel(IZMIR.id))

        async def listen() -> None:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    self.events.append(json.loads(message["data"]))

        self._listener = asyncio.create_task(listen())
        await asyncio.sleep(0.5)

    # -- simulator ------------------------------------------------------------------------
    async def fault(self, device: str, kind: str, duration_s: int) -> None:
        r = await self.sim.post(
            "/faults", json={"device": device, "kind": kind, "duration_s": duration_s}
        )
        r.raise_for_status()

    async def clear_faults(self) -> None:
        try:
            for f in (await self.sim.get("/faults")).json():
                await self.sim.delete(f"/faults/{f['device']}")
        except httpx.HTTPError:
            pass

    async def close(self) -> None:
        if self._listener:
            self._listener.cancel()
        await self.redis.aclose()
        for c in (self.rabbit, self.plain, self.sim, *self.clients.values()):
            await c.aclose()


# -- checks ---------------------------------------------------------------------------------


async def check_services(stack: Stack) -> None:
    out = compose("ps", "--all", "--format", "json").stdout
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    bad = []
    for r in rows:
        if r["Service"] == "migrate":
            if r.get("ExitCode") != 0:
                bad.append("migrate")
        elif r.get("Health") not in ("healthy", "") or r.get("State") != "running":
            bad.append(f"{r['Service']}={r.get('Health') or r.get('State')}")
    seen = {r["Service"] for r in rows}
    wanted = {
        "timescaledb",
        "mosquitto",
        "rabbitmq",
        "redis",
        "ingestion",
        "alarm",
        "api",
        "simulator",
    }
    bad += [f"{s}=missing" for s in sorted(wanted - seen)]
    record("every service is healthy (incl. redis, alarm, api)", not bad, ", ".join(bad))

    ready = await httpx.AsyncClient(timeout=10).get(f"{ALARM_HTTP}/readyz")
    record("alarm service is ready (rabbitmq, db, redis)", ready.status_code == 200, ready.text)
    docs = await stack.plain.get("/docs")
    spec = await stack.plain.get("/openapi.json")
    record(
        "Swagger and the OpenAPI document are served",
        docs.status_code == 200 and spec.status_code == 200,
    )


async def check_queue(stack: Stack) -> None:
    q = (await stack.rabbit.get("/api/queues/%2F/alarm.telemetry")).json()
    args = q.get("arguments", {})
    ok = (
        q.get("consumers") == 1
        and q.get("messages", 10**9) < 100
        and args.get("x-single-active-consumer") is True
        and args.get("x-dead-letter-exchange") == "hastori.dlx"
    )
    record(
        "alarm.telemetry: 1 consumer, shallow, dead-lettering, single active consumer",
        ok,
        f"consumers={q.get('consumers')} messages={q.get('messages')}",
    )


async def check_eval_lag(stack: Stack) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        before = histogram_buckets(
            (await c.get(f"{ALARM_HTTP}/metrics")).text, "alarm_eval_lag_seconds"
        )
        await asyncio.sleep(30)
        after = histogram_buckets(
            (await c.get(f"{ALARM_HTTP}/metrics")).text, "alarm_eval_lag_seconds"
        )
    share = share_within(before, after, 2.0)
    record(
        "alarm_eval_lag_seconds: 95 % within 2 s (last 30 s)",
        share is not None and share >= 0.95,
        f"{share}",
    )


async def check_authorization(stack: Stack, alarm_open: bool) -> None:
    ids = await discover(stack.clients["admin"])
    if alarm_open:
        record("an İzmir alarm exists for the matrix", ids.izmir_alarm is not None)
    report = await run(stack.clients, stack.plain, ids)
    for f in report.failures:
        print(f"    wrong cell: {f.name}: {f.detail}", flush=True)
    record(
        f"authorization matrix: {len(report.results)} cells",
        not report.failures,
        f"{len(report.failures)} wrong",
    )


def with_cookie(token: str | None) -> httpx.AsyncClient:
    """A client holding a refresh cookie, filed under the host and path the server sets it for (so
    the server's next Set-Cookie replaces it instead of adding a second one)."""
    client = httpx.AsyncClient(base_url=API, timeout=15)
    client.cookies.set("refresh_token", token or "", domain="127.0.0.1", path="/api/v1/auth")
    return client


async def check_sign_in(stack: Stack) -> None:
    # a fake address, so no demo account is locked out
    ghost = {"email": "ghost@demo.hastori.local", "password": "nope"}
    codes = [(await stack.plain.post("/auth/login", json=ghost)).status_code for _ in range(7)]
    record("sign-in limit: 5 failures, then 429", codes == [401] * 5 + [429, 429], str(codes))

    email, password = stack.credentials("izmir_viewer")
    c = httpx.AsyncClient(base_url=API, timeout=15)
    login = await c.post("/auth/login", json={"email": email, "password": password})
    cookie = login.cookies.get("refresh_token")
    flags = login.headers.get("set-cookie", "")
    record(
        "refresh cookie is httpOnly, SameSite=Strict, scoped to /api/v1/auth",
        bool(cookie)
        and "HttpOnly" in flags
        and "SameSite=strict" in flags
        and "/api/v1/auth" in flags,
    )
    tabs = [with_cookie(cookie) for _ in range(3)]
    results = await asyncio.gather(*[t.post("/auth/refresh") for t in tabs])
    record(
        "three tabs refreshing at once all succeed", [r.status_code for r in results] == [200] * 3
    )
    await asyncio.sleep(stack.settings.refresh_grace_s + 2)
    thief = with_cookie(cookie)
    late = await thief.post("/auth/refresh")
    record(
        "the old refresh token after the grace window is refused and ends the session",
        late.status_code == 401,
    )
    newest = tabs[0].cookies.get("refresh_token")
    owner = with_cookie(newest)
    after = await owner.post("/auth/refresh")
    record("...and the owner's newer token is revoked too", after.status_code == 401)
    for client in (c, thief, owner, *tabs):
        await client.aclose()


async def active_alarm(stack: Stack, rule_id: Any) -> dict[str, Any] | None:
    r = await stack.clients["izmir_admin"].get("/alarms", params={"state": "active", "size": 100})
    return next((a for a in r.json()["items"] if a["rule_id"] == str(rule_id)), None)


async def hot_since(stack: Stack, since: datetime) -> datetime | None:
    r = await stack.clients["izmir_viewer"].get(
        f"/devices/{KOMP1.id}/measurements",
        params={
            "metric": "temperature_c",
            "from": since.isoformat(),
            "to": datetime.now(since.tzinfo).isoformat(),
            "interval": "raw",
        },
    )
    return first_above(r.json()["points"], TEMP_RULE.threshold)


async def check_overheat_lifecycle(stack: Stack) -> str | None:
    started = datetime.now().astimezone()
    stack.events.clear()
    await stack.fault("izmir-komp-1", "overheat", 120)
    alarm = await wait_for(lambda: active_alarm(stack, TEMP_RULE.id), seconds=100)
    record(
        "overheat: a critical alarm opens", alarm is not None and alarm["severity"] == "critical"
    )
    if alarm is None:
        return None
    first = await hot_since(stack, started)
    opened = parse_time(alarm["opened_at"])
    gap = (opened - first).total_seconds() if first else None
    ok = gap is not None and OPEN_TOLERANCE_S[0] <= gap <= OPEN_TOLERANCE_S[1]
    record("...exactly 30 s after the temperature crossed 80 C", ok, f"gap={gap}")
    await check_authorization(stack, alarm_open=True)

    ack = await stack.clients["izmir_admin"].post(f"/alarms/{alarm['id']}/ack")
    record(
        "a site admin acknowledges it",
        ack.status_code == 200 and ack.json()["state"] == "acknowledged",
    )
    again = await stack.clients["izmir_admin"].post(f"/alarms/{alarm['id']}/ack")
    record("a second acknowledgement is a 409", again.status_code == 409)

    async def closed() -> dict[str, Any] | None:
        d = (await stack.clients["izmir_admin"].get(f"/alarms/{alarm['id']}")).json()
        return d if d["state"] == "cleared" else None

    final = await wait_for(closed, seconds=240, every=5)
    record("...and it closes by itself when the value recovers", final is not None)
    if final:
        steps = [e["event"] for e in final["timeline"]]
        record(
            "timeline: opened, acknowledged, cleared",
            steps == ["opened", "acknowledged", "cleared"],
            str(steps),
        )
        record(
            "peak value is the overheat peak",
            final["peak_value"] is not None and final["peak_value"] > 80,
        )
    await asyncio.sleep(1)
    kinds = event_types(stack.events, alarm["id"])
    record(
        "Redis events: opened, acknowledged, cleared (in order)",
        kinds == ["alarm.opened", "alarm.acknowledged", "alarm.cleared"],
        str(kinds),
    )
    listing = await stack.clients["izmir_admin"].get(
        "/alarms", params={"from": started.isoformat(), "size": 100}
    )
    mine = [a for a in listing.json()["items"] if a["rule_id"] == str(TEMP_RULE.id)]
    record("exactly one alarm for the excursion", len(mine) == 1, f"{len(mine)}")
    return str(alarm["id"])


async def check_spike(stack: Stack) -> None:
    started = datetime.now().astimezone()
    await stack.fault("izmir-komp-1", "spike", 30)
    await asyncio.sleep(75)
    listing = await stack.clients["izmir_admin"].get(
        "/alarms", params={"from": started.isoformat(), "size": 100}
    )
    mine = [a for a in listing.json()["items"] if a["rule_id"] == str(TEMP_RULE.id)]
    record("a single 90 C reading opens no alarm", mine == [], f"{len(mine)} alarms")


async def alarm_service_ready() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return bool((await c.get(f"{ALARM_HTTP}/readyz")).status_code == 200)
    except httpx.HTTPError:
        return False


async def check_restart_and_rule_change(stack: Stack, skip_restart: bool) -> None:
    started = datetime.now().astimezone()
    await stack.fault("izmir-komp-1", "overheat", 150)
    await asyncio.sleep(20)  # the 30 s count is in progress
    if not skip_restart:
        compose("restart", "alarm")
        up = await wait_for(alarm_service_ready, seconds=120, every=3)
        record("the alarm service restarts and is ready again", bool(up))
    alarm = await wait_for(lambda: active_alarm(stack, TEMP_RULE.id), seconds=100)
    record("an alarm opens after the restart", alarm is not None)
    if alarm is None:
        return
    first = await hot_since(stack, started)
    gap = (parse_time(alarm["opened_at"]) - first).total_seconds() if first else None
    ok = gap is not None and OPEN_TOLERANCE_S[0] <= gap <= OPEN_TOLERANCE_S[1]
    record(
        "...at the time the data says (replayed from the database), not at the restart",
        ok,
        f"gap={gap}",
    )
    listing = await stack.clients["izmir_admin"].get(
        "/alarms", params={"from": started.isoformat(), "size": 100}
    )
    mine = [a for a in listing.json()["items"] if a["rule_id"] == str(TEMP_RULE.id)]
    record("exactly one alarm for the excursion", len(mine) == 1, f"{len(mine)}")

    if not skip_restart:
        compose("restart", "alarm")  # restart with the alarm open
        await wait_for(alarm_service_ready, seconds=120, every=3)
        await asyncio.sleep(10)
        listing = await stack.clients["izmir_admin"].get(
            "/alarms", params={"from": started.isoformat(), "size": 100}
        )
        mine = [a for a in listing.json()["items"] if a["rule_id"] == str(TEMP_RULE.id)]
        record(
            "a restart with the alarm open creates no second alarm", len(mine) == 1, f"{len(mine)}"
        )

    # disabling the rule closes the alarm: proves the rule change reaches the running service
    admin = stack.clients["izmir_admin"]
    body = (await admin.get(f"/alarm-rules/{TEMP_RULE.id}")).json()
    original = {
        k: body[k]
        for k in (
            "name",
            "kind",
            "metric",
            "operator",
            "threshold",
            "clear_threshold",
            "duration_s",
            "window_s",
            "severity",
        )
    }
    try:
        t0 = time.monotonic()
        await admin.delete(f"/alarm-rules/{TEMP_RULE.id}")

        async def cleared() -> bool:
            return bool((await admin.get(f"/alarms/{alarm['id']}")).json()["state"] == "cleared")

        closed = await wait_for(cleared, seconds=10, every=0.5)
        record(
            "disabling the rule closes its alarm within seconds",
            bool(closed),
            f"{time.monotonic() - t0:.1f}s",
        )
    finally:
        await stack.clear_faults()  # the value must recover before the rule is switched on again

        async def cool() -> bool:
            r = await stack.clients["izmir_viewer"].get(f"/sites/{IZMIR.id}/devices")
            komp = next(d for d in r.json() if d["key"] == "izmir-komp-1")
            current = komp["latest"].get("temperature_c", {}).get("value", 100)
            return bool(current < TEMP_RULE.threshold - 2)

        await wait_for(cool, seconds=180, every=5)
        restored = await admin.put(
            f"/alarm-rules/{TEMP_RULE.id}", json={**original, "enabled": True}
        )
        record("the rule is switched on again", restored.status_code == 200)


async def check_dead_letter(stack: Stack) -> None:
    async def depth() -> int:
        return int(
            (await stack.rabbit.get("/api/queues/%2F/alarm.telemetry.dlq"))
            .json()
            .get("messages", 0)
        )

    before = await depth()
    payload = {
        "properties": {},
        "routing_key": "telemetry.e2e",
        "payload": "this is not json",
        "payload_encoding": "string",
    }
    r = await stack.rabbit.post("/api/exchanges/%2F/hastori.telemetry/publish", json=payload)
    routed = r.json().get("routed") if r.status_code == 200 else None

    async def grew() -> bool:
        return await depth() > before

    ok = await wait_for(grew, seconds=15, every=1)
    record(
        "a malformed message is dead-lettered",
        bool(routed) and bool(ok),
        f"routed={routed} dlq {before} -> {await depth()}",
    )
    record("...and the alarm service is still ready", await alarm_service_ready())


async def check_reactive(stack: Stack) -> None:
    if await active_alarm(stack, RATIO_RULE.id):
        record("the reactive-ratio warning is already open (from an earlier run)", True)
        return
    started = datetime.now().astimezone()
    await stack.fault("izmir-pano", "compensation_failure", 600)

    async def opened() -> dict[str, Any] | None:
        r = await stack.clients["izmir_admin"].get(
            "/alarms", params={"from": started.isoformat(), "size": 100}
        )
        return next((a for a in r.json()["items"] if a["rule_id"] == str(RATIO_RULE.id)), None)

    alarm = await wait_for(opened, seconds=300, every=10)
    took = (parse_time(alarm["opened_at"]) - started).total_seconds() if alarm else None
    record(
        "compensation failure raises the reactive-ratio warning in about 2-3 minutes",
        alarm is not None and 30 <= (took or 0) <= 270,
        f"{took}",
    )
    await stack.clear_faults()  # the alarm closes once the 10 minute window has aged out


async def check_site_views(stack: Stack) -> None:
    viewer = stack.clients["izmir_viewer"]
    devices = (await viewer.get(f"/sites/{IZMIR.id}/devices")).json()
    record(
        "all 4 devices of İzmir report values and are online",
        len(devices) == 4 and all(d["online"] and d["latest"] for d in devices),
        str([d["key"] for d in devices if not d["online"]]),
    )
    days = (await viewer.get(f"/sites/{IZMIR.id}/consumption/daily", params={"days": 2})).json()[
        "days"
    ]
    today = days[-1]
    record(
        "daily consumption: kWh from the main panel, coverage in (0, 1]",
        today["kwh"] > 0 and 0 < today["coverage"] <= 1,
        str(today),
    )


async def main() -> int:
    skip_reactive, skip_restart = "--skip-reactive" in sys.argv, "--skip-restart" in sys.argv
    stack = Stack()
    steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
    try:
        await check_services(stack)
        for actor in ACTORS:
            stack.clients[actor] = await stack.sign_in(actor)
        await stack.clear_faults()
        await stack.start_events()
        steps = [
            ("queue", lambda: check_queue(stack)),
            ("site views", lambda: check_site_views(stack)),
            ("sign-in", lambda: check_sign_in(stack)),
            ("eval lag", lambda: check_eval_lag(stack)),
            ("overheat lifecycle", lambda: check_overheat_lifecycle(stack)),
            ("spike", lambda: check_spike(stack)),
            ("restart and rule change", lambda: check_restart_and_rule_change(stack, skip_restart)),
            ("dead letter", lambda: check_dead_letter(stack)),
        ]
        if not skip_reactive:
            steps.append(("reactive ratio", lambda: check_reactive(stack)))
        for name, step in steps:
            try:
                await step()
            except Exception as exc:  # one broken step must not hide the others
                record(f"step '{name}' ran to the end", False, f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        record("the stack is reachable", False, f"{type(exc).__name__}: {exc}")
    finally:
        await stack.clear_faults()
        await stack.close()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
