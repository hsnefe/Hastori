"""Outage drills: proves no measurement is lost when a part of the stack goes away.

Needs the full stack with the simulator running. Takes about 15 minutes (about 5 with --quick).
Each drill stops or kills a service, lets the simulator keep publishing, brings the service back
and then checks every one of the 28 series for gaps in the affected window.

Drills: graceful restart, kill -9 (the real test of "ack only after commit"), an outage longer
than the 5 minutes the old age limit allowed (6 minutes; skipped by --quick), broker restart,
database outage, RabbitMQ outage, SIGTERM with the database down. Whatever happens, the services
are started again at the end.

    make resilience            # everything
    make resilience ARGS=--quick
"""

import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

import asyncpg

from hastori_common.seed_data import load_seed
from hastori_common.settings import ROOT, Settings, get_settings

SAMPLE_INTERVAL_S = 2.0
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
    for service in ("timescaledb", "rabbitmq", "mosquitto", "ingestion"):
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


async def main() -> None:
    s = get_settings()
    quick = "--quick" in sys.argv
    if not await wait_ready(s, 10):
        sys.exit("stack is not ready: make up && make seed && make simulate")
    drills = [
        drill_ingestion_restart,
        drill_ingestion_kill,
        *([] if quick else [drill_long_ingestion_outage]),
        drill_broker_restart,
        drill_db_outage,
        drill_rabbitmq_outage,
        drill_sigterm_with_db_down,
    ]
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
