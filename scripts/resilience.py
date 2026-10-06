"""Outage drills: proves no measurement is lost when a part of the stack goes away.

Needs the full stack with the simulator running. Takes about 5 minutes. Each drill stops a
service, lets the simulator keep publishing, brings the service back and then checks every one of
the 28 series for gaps in the affected window.

    make resilience
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
from hastori_common.settings import Settings, get_settings

SAMPLE_INTERVAL_S = 2.0
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "--profile", "sim", *args], check=True, capture_output=True
    )


def compose_logs(service: str, since: str) -> str:
    out = subprocess.run(
        ["docker", "compose", "logs", service, "--since", since],
        capture_output=True,
        text=True,
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
    record("rabbitmq down: events wait in the outbox", queued > 0, f"{queued} events queued")
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
    if not await wait_ready(s, 10):
        sys.exit("stack is not ready: make up && make seed && make simulate")
    for drill in (
        drill_ingestion_restart,
        drill_db_outage,
        drill_rabbitmq_outage,
        drill_sigterm_with_db_down,
    ):
        print(f"--- {drill.__name__}", flush=True)
        await drill(s)
        await wait_ready(s, 120)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} drills passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
