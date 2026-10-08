"""Outage drills: proves no measurement is lost and no alarm is missed or doubled when a part of
the stack goes away.

Needs the full stack with the simulator running. Takes about 30 minutes (about 20 with --quick).
The ingestion drills stop or kill a service, let the simulator keep publishing, bring the service
back and then check every one of the 28 series for gaps in the affected window.

Ingestion drills: graceful restart, kill -9 (the real test of "ack only after commit"), an outage
longer than the 5 minutes the old age limit allowed (6 minutes; skipped by --quick), broker
restart, database outage, RabbitMQ outage, SIGTERM with the database down.

Alarm drills: an overheat fault is injected, the disruption hits while the rule is still counting
its 30 s, and the drill then checks that exactly one alarm opened (not zero, not two), that it
opened at the time the data says (the state is a function of stored data, not of memory), that it
closed again and that the alarm queue drained. Disruptions: RabbitMQ restart, kill -9 of the alarm
service, a database outage, and the process exiting by itself (SIGTERM to PID 1), which
`restart: unless-stopped` has to bring back (`docker kill` counts as a manual stop and does not).

Whatever happens, the services are started again at the end.

    make resilience            # everything
    make resilience ARGS=--quick
    make resilience ARGS=--alarm-only   # just the alarm service drills (about 10 minutes)
"""

import asyncio
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import asyncpg

from hastori_common.seed_data import load_seed
from hastori_common.settings import ROOT, Settings, get_settings

SAMPLE_INTERVAL_S = 2.0
ALARM_DEVICE = "izmir-komp-1"
# The overheat crosses 80 C after 12-16 s, the demo rule wants 30 s above it: the alarm opens
# 42-46 s after the fault starts. 75 s lets the fault end, so the alarm closes by itself.
ALARM_FAULT_S = 75
ALARM_OPENS_AFTER_S = (38.0, 62.0)
# A drill that hangs (a service that never comes back, a docker call that never returns) must
# fail the run, not freeze it: every docker call, every drill and the whole run have a deadline.
COMMAND_TIMEOUT_S = 180
DRILL_TIMEOUT_S = {
    "drill_long_ingestion_outage": 1200,
    "drill_sigterm_with_db_down": 600,
}
DEFAULT_DRILL_TIMEOUT_S = 420
TOTAL_TIMEOUT_S = 2700
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "--profile", "sim", *args],
        check=True,
        capture_output=True,
        cwd=ROOT,
        timeout=COMMAND_TIMEOUT_S,
    )


def compose_logs(service: str, since: str) -> str:
    out = subprocess.run(
        ["docker", "compose", "logs", service, "--since", since],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=COMMAND_TIMEOUT_S,
    )
    return out.stdout + out.stderr


def readyz(s: Settings) -> dict[str, bool] | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{s.ingest_http_port}/readyz", timeout=3
        ) as r:
            return json.loads(r.read())  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())  # type: ignore[no-any-return]
    except OSError:
        return None


async def wait_ready(s: Settings, max_wait_s: float = 90) -> bool:
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        status = readyz(s)
        if status and all(status.values()):
            return True
        await asyncio.sleep(2)
    return False


async def connect(s: Settings, retries: int = 30) -> asyncpg.Connection:
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    for _ in range(retries):
        try:
            return await asyncpg.connect(dsn)
        except (OSError, asyncpg.PostgresError):
            await asyncio.sleep(2)
    raise RuntimeError("database did not come back")


async def gaps(s: Settings, start: float, end: float) -> tuple[int, int]:
    """(series with a gap, series checked) in [start, end]; allows one missing sample."""
    conn = await connect(s)
    try:
        expected = int((end - start) / SAMPLE_INTERVAL_S)
        rows = await conn.fetch(
            "SELECT device_id, metric, count(*) AS n FROM measurements "
            "WHERE time >= $1 AND time < $2 GROUP BY 1, 2",
            datetime.fromtimestamp(start, UTC),
            datetime.fromtimestamp(end, UTC),
        )
    finally:
        await conn.close()
    series = len(load_seed().devices) * 4
    short = series - sum(1 for r in rows if r["n"] >= expected - 1)
    return short, series


def start_all() -> None:
    for service in ("timescaledb", "rabbitmq", "mosquitto", "ingestion", "alarm"):
        try:
            subprocess.run(
                ["docker", "compose", "--profile", "sim", "start", service],
                capture_output=True,
                cwd=ROOT,
                timeout=COMMAND_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"could not start {service}: {exc}", flush=True)


async def wait_no_gaps(
    s: Settings, start: float, end: float, timeout_s: float = 300
) -> tuple[int, int]:
    """Poll until no series has a gap (a replayed backlog takes time to drain) or timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        short, total = await gaps(s, start, end)
        if short == 0 or time.monotonic() > deadline:
            return short, total
        await asyncio.sleep(10)


def mqtt_ready(s: Settings) -> bool:
    return bool((readyz(s) or {}).get("mqtt"))


def connects(s: Settings) -> float:
    with urllib.request.urlopen(f"http://127.0.0.1:{s.ingest_http_port}/metrics", timeout=5) as r:
        for line in r.read().decode().splitlines():
            if line.startswith("ingest_mqtt_connects_total"):
                return float(line.split()[-1])
    return 0.0


async def drill_ingestion_restart(s: Settings) -> None:
    t0 = time.time()
    compose("stop", "ingestion")
    await asyncio.sleep(25)
    compose("start", "ingestion")
    ok = await wait_ready(s)
    await asyncio.sleep(15)  # let the backlog drain
    short, total = await gaps(s, t0 + 2, t0 + 35)
    record(
        "ingestion down 25 s: no gaps after restart",
        ok and short == 0,
        f"{short}/{total} series with gaps",
    )


async def drill_ingestion_kill(s: Settings) -> None:
    """SIGKILL: no drain, no final ack. Whatever was committed but not yet acked is redelivered
    and absorbed by ON CONFLICT; whatever was never committed is redelivered and written."""
    t0 = time.time()
    compose("kill", "-s", "SIGKILL", "ingestion")
    await asyncio.sleep(15)
    compose("start", "ingestion")
    ok = await wait_ready(s)
    short, total = await wait_no_gaps(s, t0 + 2, t0 + 25, 120)
    record(
        "ingestion killed (SIGKILL) for 15 s: no gaps, no duplicates needed",
        ok and short == 0,
        f"{short}/{total} series with gaps",
    )


async def drill_long_ingestion_outage(s: Settings) -> None:
    """Longer than the 5 minute age limit the parser used to have: the backlog the broker
    replays is old, and must still be stored."""
    t0 = time.time()
    compose("stop", "ingestion")
    await asyncio.sleep(360)
    compose("start", "ingestion")
    ok = await wait_ready(s)
    short, total = await wait_no_gaps(s, t0 + 2, t0 + 355, 420)
    record(
        "ingestion down 6 min: broker backlog is replayed, no gaps",
        ok and short == 0,
        f"{short}/{total} series with gaps",
    )


async def drill_broker_restart(s: Settings) -> None:
    """The simulator only buffers ~10 s per device, so this drill checks recovery (reconnect,
    data flowing again), not gap-freedom."""
    before = connects(s)
    compose("restart", "mosquitto")
    await asyncio.sleep(5)
    deadline = time.monotonic() + 90
    recovered = False
    while time.monotonic() < deadline and not recovered:
        recovered = mqtt_ready(s)
        if not recovered:
            await asyncio.sleep(2)
    t_ok = time.time()
    await asyncio.sleep(20)
    conn = await connect(s)
    fresh = await conn.fetchval(
        "SELECT count(*) FROM measurements WHERE time > $1", datetime.fromtimestamp(t_ok, UTC)
    )
    await conn.close()
    record(
        "mosquitto restart: ingestion reconnects and data flows again",
        recovered and fresh > 0 and connects(s) > before,
        f"mqtt ready={recovered}, {fresh} new rows, connects {before:.0f} -> {connects(s):.0f}",
    )


async def drill_db_outage(s: Settings) -> None:
    t0 = time.time()
    compose("stop", "timescaledb")
    await asyncio.sleep(20)
    status = readyz(s)
    record(
        "db down: service stays up, readyz reports it",
        status is not None and status["db"] is False,
        str(status),
    )
    compose("start", "timescaledb")
    await wait_ready(s, 120)
    await asyncio.sleep(25)
    short, total = await gaps(s, t0 + 2, t0 + 30)
    record("db down 20 s: no gaps after recovery", short == 0, f"{short}/{total} series with gaps")


async def drill_rabbitmq_outage(s: Settings) -> None:
    compose("stop", "rabbitmq")
    await asyncio.sleep(20)
    conn = await connect(s)
    queued = await conn.fetchval("SELECT count(*) FROM outbox")
    await conn.close()
    status = readyz(s)
    record(
        "rabbitmq down: events wait in the outbox, readyz reports it",
        queued > 0 and status is not None and status["rabbitmq"] is False,
        f"{queued} events queued, {status}",
    )
    compose("start", "rabbitmq")
    await wait_ready(s, 120)
    await asyncio.sleep(25)
    conn = await connect(s)
    left = await conn.fetchval("SELECT count(*) FROM outbox")
    await conn.close()
    record("rabbitmq back: outbox drains", left < 100, f"{left} events left")


async def drill_sigterm_with_db_down(s: Settings) -> None:
    t0 = time.time()
    compose("stop", "timescaledb")
    await asyncio.sleep(8)
    started = time.monotonic()
    compose("stop", "ingestion")  # SIGTERM: must give up at its deadline, un-acked
    took = time.monotonic() - started
    logs = compose_logs("ingestion", "2m")
    record(
        "sigterm with db down: exits within the grace period",
        took < 40,
        f"{took:.0f} s, deadline message logged: {'shutdown deadline' in logs}",
    )
    compose("start", "timescaledb")
    await asyncio.sleep(10)
    compose("start", "ingestion")
    await wait_ready(s, 120)
    await asyncio.sleep(20)
    short, total = await gaps(s, t0 + 2, t0 + 30)
    record(
        "sigterm with db down: nothing lost (redelivered)",
        short == 0,
        f"{short}/{total} series with gaps",
    )


# -- alarm service drills ---------------------------------------------------------------------


def alarm_ready(s: Settings) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{s.alarm_http_port}/readyz", timeout=3) as r:
            return bool(r.status == 200)
    except OSError:  # connection refused, timeout, 503 (HTTPError is an OSError)
        return False


async def wait_alarm_ready(s: Settings, max_wait_s: float = 120) -> bool:
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        if alarm_ready(s):
            return True
        await asyncio.sleep(2)
    return False


def inject_fault(s: Settings, kind: str, duration_s: float) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{s.sim_control_port}/faults",
        data=json.dumps({"device": ALARM_DEVICE, "kind": kind, "duration_s": duration_s}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {s.sim_control_token}",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()


def queue_depth(s: Settings) -> int | None:
    m = re.match(r"amqp://([^:]+):([^@]+)@", s.rabbitmq_url)
    user, password = (m.group(1), m.group(2)) if m else ("hastori", "hastori_demo")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(
        "http://127.0.0.1:15672/api/queues/%2F/alarm.telemetry",
        headers={"Authorization": f"Basic {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(json.loads(r.read()).get("messages", 0))
    except (OSError, ValueError):
        return None


async def fetch(s: Settings, sql: str, *args: object) -> list[asyncpg.Record]:
    """One query on a fresh connection: a drill breaks the database on purpose, and a connection
    held across the break is a dead one."""
    conn = await connect(s)
    try:
        rows: list[asyncpg.Record] = await conn.fetch(sql, *args)
        return rows
    finally:
        await conn.close()


async def alarms_since(s: Settings, rule_id: object, since: float) -> list[asyncpg.Record]:
    return await fetch(
        s,
        "SELECT state, opened_at, cleared_at FROM alarms WHERE rule_id = $1 AND opened_at >= $2 "
        "ORDER BY opened_at",
        rule_id,
        datetime.fromtimestamp(since, UTC),
    )


async def wait_device_quiet(s: Settings, rule_id: object, max_wait_s: float = 150) -> bool:
    """No open alarm for the rule and the device already below the clear threshold: an earlier
    fault (an earlier drill, a manual `make fault`) must be over before the next one starts."""
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        (open_now,) = await fetch(
            s, "SELECT count(*) AS n FROM alarms WHERE rule_id = $1 AND state <> 'cleared'", rule_id
        )
        latest = await fetch(
            s,
            "SELECT m.value FROM measurements m JOIN devices d ON d.id = m.device_id "
            "WHERE d.key = $1 AND m.metric = 'temperature_c' ORDER BY m.time DESC LIMIT 1",
            ALARM_DEVICE,
        )
        if open_now["n"] == 0 and latest and latest[0]["value"] < 75.0:
            return True
        await asyncio.sleep(2)
    return False


async def alarm_drill(
    s: Settings, label: str, disrupt: Callable[[], Awaitable[None]], *, wait_for: float = 14
) -> None:
    """Inject an overheat, disrupt `wait_for` seconds in (the rule is still counting), then expect
    exactly one alarm, opened when the data says, closed again, and an empty queue."""
    rows = await fetch(
        s,
        "SELECT r.id FROM alarm_rules r JOIN devices d ON d.id = r.device_id "
        "WHERE d.key = $1 AND r.metric = 'temperature_c' AND r.enabled",
        ALARM_DEVICE,
    )
    rule_id = rows[0]["id"]
    if not await wait_device_quiet(s, rule_id):
        record(label, False, "an earlier fault or alarm was still running before the drill")
        return
    t0 = time.time()
    inject_fault(s, "overheat", ALARM_FAULT_S)
    await asyncio.sleep(wait_for)
    await disrupt()
    await wait_alarm_ready(s)
    found: list[asyncpg.Record] = []
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline and not found:
        found = await alarms_since(s, rule_id, t0)
        if not found:
            await asyncio.sleep(2)
    closed = False
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and found and not closed:
        found = await alarms_since(s, rule_id, t0)
        closed = bool(found) and all(a["state"] == "cleared" for a in found)
        if not closed:
            await asyncio.sleep(2)
    await asyncio.sleep(5)  # a duplicate would have been written by now
    found = await alarms_since(s, rule_id, t0)
    depth = queue_depth(s)
    delay = found[0]["opened_at"].timestamp() - t0 if found else None
    on_time = delay is not None and ALARM_OPENS_AFTER_S[0] <= delay <= ALARM_OPENS_AFTER_S[1]
    shown = "never" if delay is None else f"{delay:.0f} s"
    record(
        label,
        len(found) == 1 and on_time and closed and depth is not None and depth < 100,
        f"{len(found)} alarm(s), opened {shown} after the fault, closed={closed}, queue={depth}",
    )


async def drill_alarm_rabbitmq_restart(s: Settings) -> None:
    async def disrupt() -> None:
        compose("restart", "rabbitmq")

    await alarm_drill(s, "alarm: rabbitmq restart while an excursion is pending", disrupt)


async def drill_alarm_kill(s: Settings) -> None:
    async def disrupt() -> None:
        compose("kill", "-s", "SIGKILL", "alarm")  # a manual kill: unless-stopped does not apply
        await asyncio.sleep(10)
        compose("start", "alarm")

    await alarm_drill(s, "alarm: kill -9 while an excursion is pending", disrupt)


async def drill_alarm_db_outage(s: Settings) -> None:
    async def disrupt() -> None:
        compose("stop", "timescaledb")
        await asyncio.sleep(20)
        compose("start", "timescaledb")
        await wait_ready(s, 120)

    await alarm_drill(s, "alarm: database outage while an excursion is pending", disrupt)


def restart_count() -> int:
    container = subprocess.run(
        ["docker", "compose", "ps", "-q", "alarm"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=COMMAND_TIMEOUT_S,
    ).stdout.strip()
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
    )
    return int(out.stdout.strip() or -1)


async def drill_alarm_exits_by_itself(s: Settings) -> None:
    """The process exits on its own (SIGTERM to PID 1, as a crash or an OOM would end it):
    `restart: unless-stopped` brings it back with no `docker compose start`."""
    before = restart_count()
    restarted = False

    async def disrupt() -> None:
        nonlocal restarted
        pid1_exit = "import os, signal; os.kill(1, signal.SIGTERM)"
        compose("exec", "-T", "alarm", "python", "-c", pid1_exit)
        await asyncio.sleep(5)
        restarted = await wait_alarm_ready(s, 120) and restart_count() > before

    await alarm_drill(s, "alarm: exits by itself, unless-stopped brings it back", disrupt)
    record("alarm: docker's restart counter went up (the drill did not start it)", restarted)


async def main() -> None:
    s = get_settings()
    quick = "--quick" in sys.argv
    if not await wait_ready(s, 10):
        sys.exit("stack is not ready: make up && make seed && make simulate")
    alarm_drills = [
        drill_alarm_rabbitmq_restart,
        drill_alarm_kill,
        drill_alarm_db_outage,
        drill_alarm_exits_by_itself,
    ]
    drills = [
        drill_ingestion_restart,
        drill_ingestion_kill,
        *([] if quick else [drill_long_ingestion_outage]),
        drill_broker_restart,
        drill_db_outage,
        drill_rabbitmq_outage,
        drill_sigterm_with_db_down,
        *alarm_drills,
    ]
    if "--alarm-only" in sys.argv:
        drills = alarm_drills
    started = time.monotonic()
    try:
        for drill in drills:
            print(f"--- {drill.__name__}", flush=True)
            limit = DRILL_TIMEOUT_S.get(drill.__name__, DEFAULT_DRILL_TIMEOUT_S)
            if time.monotonic() - started > TOTAL_TIMEOUT_S:
                record(drill.__name__, False, "skipped: total time limit reached")
                continue
            try:
                await asyncio.wait_for(drill(s), limit)
            except TimeoutError:
                record(drill.__name__, False, f"timed out after {limit} s")
            except Exception as exc:  # a drill that blows up is a failed drill, not a crash
                record(drill.__name__, False, f"{type(exc).__name__}: {exc}")
            await wait_ready(s, 120)
    finally:
        start_all()  # never leave the stack half stopped, whatever happened above
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} drills passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
