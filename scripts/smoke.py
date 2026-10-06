"""Day-1 smoke check: automates the 'definition of done' list. Exit code 1 if any check fails.

Needs the stack running with the simulator: make up && make seed && make simulate

--no-fault skips the overheat check, which injects a real overheat fault into izmir-komp-1
(use it whenever alarms are being tested). The fault is cleared as soon as the temperature has
been seen above 80 C, and in any case when the check ends.

The checks that write test data (duplicate message) delete it again afterwards.
"""

import asyncio
import base64
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
from cryptography import x509

from hastori_common.seed_data import SeedData, derive_device_password, load_seed
from hastori_common.settings import ROOT, Settings, get_settings
from hastori_ingestion.parsing import message_id
from hastori_simulator.signals import DEFAULT_FAULT_S

ROOT_COUNT_TABLES = ("organizations", "sites", "users", "user_sites", "devices", "alarm_rules")
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run(cmd: list[str], input_: str | None = None) -> subprocess.CompletedProcess[str]:
    # cwd: compose and uv must find the project whatever directory the script is started from
    return subprocess.run(cmd, capture_output=True, text=True, input=input_, timeout=120, cwd=ROOT)


def http_get(url: str, auth: tuple[str, str] | None = None) -> str:
    req = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return str(resp.read().decode())


def metric_value(text: str, name: str, labels: str = "") -> float:
    m = re.search(rf"^{re.escape(name)}{re.escape(labels)}\s+([0-9.e+-]+)$", text, re.M)
    return float(m.group(1)) if m else 0.0


def mqtt_pub(
    s: Settings, user: str, password: str, topic: str, payload: str
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker", "compose", "exec", "-T", "mosquitto", "mosquitto_pub", "-V", "5", "-q", "1",
            "-h", "localhost", "-p", "8883", "--cafile", "/mosquitto/certs/ca.crt",
            "-u", user, "-P", password, "-t", topic, "-m", payload,
        ]
    )  # fmt: skip


async def check_compose() -> None:
    # --all: the one-shot `migrate` has exited and is hidden from a plain `ps`
    out = run(["docker", "compose", "--profile", "sim", "ps", "--all", "--format", "json"]).stdout
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    bad = []
    for r in rows:
        if r["Service"] == "migrate":
            if r.get("ExitCode") != 0:
                bad.append("migrate")
        elif r.get("Health") not in ("healthy", ""):
            bad.append(f"{r['Service']}={r.get('Health')}")
        elif r.get("State") != "running":
            bad.append(f"{r['Service']}={r.get('State')}")
    seen = {r["Service"] for r in rows}
    bad += [f"{name}=missing" for name in ("migrate", "ingestion", "mosquitto") if name not in seen]
    record("docker compose: all services healthy", bool(rows) and not bad, ", ".join(bad))


SNAPSHOT_QUERIES = {
    "users": "SELECT id, email, role, password_hash FROM users ORDER BY id",
    "user_sites": "SELECT user_id, site_id FROM user_sites ORDER BY 1, 2",
    "devices": "SELECT id, key, name, type, mqtt_topic, is_active FROM devices ORDER BY id",
    "alarm_rules": "SELECT * FROM alarm_rules ORDER BY id",
}


async def counts(conn: asyncpg.Connection) -> dict[str, object]:
    """Row counts plus the content of the tables the seed writes: an idempotent seed changes
    neither (a count alone would miss a rewritten name or password hash)."""
    out: dict[str, object] = {
        t: await conn.fetchval(f"SELECT count(*) FROM {t}")  # noqa: S608
        for t in ROOT_COUNT_TABLES
    }
    for name, query in SNAPSHOT_QUERIES.items():
        out[f"{name}_content"] = [tuple(r.values()) for r in await conn.fetch(query)]
    return out


async def check_seed(conn: asyncpg.Connection) -> None:
    before = await counts(conn)
    res = run(["uv", "run", "python", "scripts/seed.py"])
    after = await counts(conn)
    record("seed is idempotent", res.returncode == 0 and before == after, res.stderr[-200:])


async def check_series(conn: asyncpg.Connection, expected: int) -> None:
    deadline = time.monotonic() + 60
    n = 0
    while time.monotonic() < deadline:
        n = await conn.fetchval(
            "SELECT count(*) FROM (SELECT DISTINCT device_id, metric FROM measurements "
            "WHERE time > now() - interval '60 seconds') s"
        )
        if n >= expected:
            break
        await asyncio.sleep(3)
    record("all 28 series have data within 60 s", n == expected, f"{n}/{expected}")


WINDOW_S = 30.0
SAMPLE_INTERVAL_S = 2.0


def rabbit_publish_total(s: Settings) -> float:
    user, pw = "hastori", "hastori_demo"
    m = re.match(r"amqp://([^:]+):([^@]+)@", s.rabbitmq_url)
    if m:
        user, pw = m.group(1), m.group(2)
    data = json.loads(
        http_get("http://127.0.0.1:15672/api/exchanges/%2F/hastori.telemetry", (user, pw))
    )
    return float(data.get("message_stats", {}).get("publish_in", 0))


def snapshot(s: Settings) -> tuple[float, str, float]:
    text = http_get(f"http://127.0.0.1:{s.ingest_http_port}/metrics")
    return time.monotonic(), text, rabbit_publish_total(s)


def check_window(s: Settings, before: tuple[float, str, float], n_devices: int) -> None:
    """Lag p95 and RabbitMQ rate, both measured over the same recent window (not since boot)."""
    t0, m0, r0 = before
    t1, m1, r1 = snapshot(s)
    count = metric_value(m1, "ingest_lag_seconds_count")
    count -= metric_value(m0, "ingest_lag_seconds_count")
    le2 = metric_value(m1, "ingest_lag_seconds_bucket", '{le="2.0"}') - metric_value(
        m0, "ingest_lag_seconds_bucket", '{le="2.0"}'
    )
    ratio = le2 / count if count else 0.0
    record(
        "ingest lag p95 < 2 s",
        count > 0 and ratio >= 0.95,
        f"{ratio:.1%} of {int(count)} <= 2 s in the last {t1 - t0:.0f} s",
    )
    rate = (r1 - r0) / (t1 - t0)
    expected = n_devices / SAMPLE_INTERVAL_S  # one message per device per sample
    record(
        f"rabbitmq exchange receives ~{expected:.1f} msg/s",
        0.8 * expected <= rate <= 1.2 * expected,
        f"{rate:.2f} msg/s",
    )


async def check_aggregate(conn: asyncpg.Connection) -> None:
    n = await conn.fetchval(
        "SELECT count(*) FROM measurements_1m WHERE bucket > now() - interval '3 minutes'"
    )
    record("measurements_1m returns recent rows", n > 0, f"{n} rows")


async def check_duplicate(conn: asyncpg.Connection, s: Settings, seed: SeedData) -> None:
    dev = seed.device_by_key("izmir-komp-1")
    topic = seed.topic(dev)
    pw = derive_device_password(s.mqtt_device_secret, dev.id)
    ts = int(time.time()) - 100
    body = json.dumps({"ts": ts, "metrics": {"current_a": 42.0}})
    for _ in range(2):
        mqtt_pub(s, str(dev.id), pw, topic, body)
    await asyncio.sleep(3)
    n = await conn.fetchval(
        "SELECT count(*) FROM measurements WHERE device_id=$1 AND metric='current_a' AND time=$2",
        dev.id,
        datetime.fromtimestamp(ts, UTC),
    )
    record("duplicate message yields a single row", n == 1, f"{n} rows")
    # Do not leave a fake reading in the real series (it would show up in charts and alarms).
    await conn.execute(
        "DELETE FROM measurements WHERE device_id=$1 AND metric='current_a' AND time=$2",
        dev.id,
        datetime.fromtimestamp(ts, UTC),
    )
    await conn.execute("DELETE FROM outbox WHERE message_id=$1", message_id(dev.id, float(ts)))


async def check_bad_payload(s: Settings, seed: SeedData) -> None:
    dev = seed.device_by_key("izmir-komp-1")
    pw = derive_device_password(s.mqtt_device_secret, dev.id)
    url = f"http://127.0.0.1:{s.ingest_http_port}"
    before = metric_value(
        http_get(f"{url}/metrics"), "ingest_rejected_total", '{reason="invalid_json"}'
    )
    mqtt_pub(s, str(dev.id), pw, seed.topic(dev), "this is not json")
    await asyncio.sleep(2)
    after = metric_value(
        http_get(f"{url}/metrics"), "ingest_rejected_total", '{reason="invalid_json"}'
    )
    alive = json.loads(http_get(f"{url}/healthz")).get("status") == "ok"
    record("bad payload rejected, service alive", after > before and alive, f"{before} -> {after}")


def _mosquitto_pub(*args: str) -> subprocess.CompletedProcess[str]:
    return run(
        ["docker", "compose", "exec", "-T", "mosquitto", "mosquitto_pub", "-h", "localhost",
         "-p", "8883", *args]
    )  # fmt: skip


def check_security(s: Settings, seed: SeedData) -> None:
    a = seed.device_by_key("izmir-komp-1")
    b = seed.device_by_key("izmir-komp-2")
    pw = derive_device_password(s.mqtt_device_secret, a.id)
    ca = ["--cafile", "/mosquitto/certs/ca.crt"]
    own = ["-t", seed.topic(a), "-m", "{}"]

    # Positive control first: the very same command with correct credentials and TLS works.
    # Without it, "refused" below could just mean `docker exec` itself failed.
    good = _mosquitto_pub(*ca, "-u", str(a.id), "-P", pw, *own)
    record(
        "control: valid credentials + TLS can publish to own topic",
        good.returncode == 0,
        (good.stdout + good.stderr).strip()[:80],
    )

    cross = mqtt_pub(s, str(a.id), pw, seed.topic(b), "{}")
    denied = "Not authorized" in (cross.stdout + cross.stderr)
    record("cross-device topic write is denied", denied, (cross.stdout + cross.stderr).strip()[:80])

    anon = _mosquitto_pub(*ca, *own)
    msg = (anon.stdout + anon.stderr).lower()
    record(
        "anonymous connection is refused",
        anon.returncode != 0 and ("not authori" in msg or "refused" in msg),
        msg.strip()[:80],
    )

    plain = _mosquitto_pub("-u", str(a.id), "-P", pw, *own)
    msg = (plain.stdout + plain.stderr).lower()
    record(
        "connection without TLS is refused",
        plain.returncode != 0
        and (
            "tls" in msg
            or "ssl" in msg
            or "protocol" in msg
            or "connection error" in msg
            or "unable" in msg
        ),
        msg.strip()[:80],
    )


def check_session_hijack(s: Settings, seed: SeedData) -> None:
    """A device user connecting with the ingestion client id must not take its session over."""
    url = f"http://127.0.0.1:{s.ingest_http_port}"
    before = metric_value(http_get(f"{url}/metrics"), "ingest_mqtt_connects_total")
    a = seed.device_by_key("izmir-komp-1")
    pw = derive_device_password(s.mqtt_device_secret, a.id)
    _mosquitto_pub(
        "--cafile",
        "/mosquitto/certs/ca.crt",
        "-V",
        "5",
        "-i",
        s.mqtt_ingestion_user,
        "-u",
        str(a.id),
        "-P",
        pw,
        "-t",
        seed.topic(a),
        "-m",
        "{}",
    )
    time.sleep(3)
    after = metric_value(http_get(f"{url}/metrics"), "ingest_mqtt_connects_total")
    ready = json.loads(http_get(f"{url}/readyz")).get("mqtt") is True
    record(
        "device cannot hijack the ingestion MQTT session",
        after == before and ready,
        f"connects {before:.0f} -> {after:.0f}",
    )


def check_ca_key_not_mounted() -> None:
    mounted = ROOT / "infra" / "mosquitto" / "certs"
    leaked = sorted(p.name for p in mounted.glob("*.key") if p.name != "server.key")
    record(
        "CA private key is not in the directory the broker can read", not leaked, ", ".join(leaked)
    )


def _control(s: Settings, method: str, path: str, body: dict[str, object] | None = None) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{s.sim_control_port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {s.sim_control_token}",
        },
        method=method,
    )
    urllib.request.urlopen(req, timeout=5).read()  # noqa: ASYNC210


async def check_overheat(conn: asyncpg.Connection, s: Settings, seed: SeedData) -> None:
    dev = seed.device_by_key("izmir-komp-1")
    start = datetime.now(UTC)
    _control(
        s, "POST", "/faults", {"device": dev.key, "kind": "overheat", "duration_s": DEFAULT_FAULT_S}
    )
    peak = 0.0
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and peak <= 80:
            await asyncio.sleep(3)
            peak = await conn.fetchval(
                "SELECT coalesce(max(value), 0) FROM measurements "
                "WHERE device_id=$1 AND metric='temperature_c' AND time > $2",
                dev.id,
                start,
            )
    finally:
        _control(s, "DELETE", f"/faults/{dev.key}")  # leave no fault behind for later tests
    record("overheat fault produces > 80 C within 30 s", peak > 80, f"peak {peak:.1f}")


def check_certificate() -> None:
    cert_file = Path(__file__).resolve().parents[1] / "infra/mosquitto/certs/server.crt"
    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    days = (cert.not_valid_after_utc - datetime.now(UTC)).days
    record("broker certificate valid for > 30 days", days > 30, f"{days} days left")


def check_git() -> None:
    tracked = run(["git", "ls-files"]).stdout.splitlines()
    leaked = [
        f
        for f in tracked
        if f == ".env" or f.endswith((".key", "passwd", "/acl")) or "/certs/" in f
    ]
    record("no secrets tracked in git", not leaked, ", ".join(leaked))


async def main() -> None:
    run_fault = "--no-fault" not in sys.argv
    s = get_settings()
    seed = load_seed()
    await check_compose()
    check_certificate()
    conn = await asyncpg.connect(s.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await check_seed(conn)
        await check_series(conn, len(seed.devices) * 4)
        window_start = snapshot(s)
        await check_aggregate(conn)
        await check_duplicate(conn, s, seed)
        await check_bad_payload(s, seed)
        check_security(s, seed)
        check_session_hijack(s, seed)
        check_ca_key_not_mounted()
        await asyncio.sleep(max(0.0, WINDOW_S - (time.monotonic() - window_start[0])))
        check_window(s, window_start, len(seed.devices))
        if run_fault:
            await check_overheat(conn, s, seed)
        else:
            print("[SKIP] overheat fault check (--no-fault)")
    finally:
        await conn.close()
    check_git()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
