"""Tenant isolation and roles, endpoint by endpoint (scripts/api_matrix.py), plus the structural
rules that keep it that way."""

import inspect
import re
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from conftest import ApiHarness, load_script
from hastori_api import queries
from hastori_api.errors import not_found
from hastori_api.queries import alarms, consumption, devices, measurements, rules, sites, users
from hastori_common.seed_data import load_seed

matrix = load_script("api_matrix")
SEED = load_seed()
API_SRC = Path(__file__).resolve().parents[1] / "src" / "hastori_api"


async def add_alarm(dsn: str, rule_key: str, device_key: str) -> str:
    """An open alarm for one of the seeded rules."""
    rule = next(r for r in SEED.alarm_rules if r.device == rule_key)
    device = SEED.device_by_key(device_key)
    alarm_id = uuid.uuid4()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO alarms (id, rule_id, device_id, state, opened_at, peak_value) "
            "VALUES ($1, $2, $3, 'active', now() - interval '2 minutes', 88.0)",
            alarm_id,
            rule.id,
            device.id,
        )
    finally:
        await conn.close()
    return str(alarm_id)


async def everyone(api: ApiHarness) -> dict[str, Any]:
    return {who: await api.signed_in(who) for who in matrix.ACTORS}


async def run_matrix(api: ApiHarness) -> Any:
    await add_alarm(api.dsn, "izmir-komp-1", "izmir-komp-1")
    await add_alarm(api.dsn, "antalya-pano", "antalya-pano")
    clients = await everyone(api)
    ids = await matrix.discover(clients["admin"])
    assert ids.izmir_alarm and ids.antalya_alarm
    return await matrix.run(clients, api.client(), ids)


async def test_every_endpoint_gives_every_user_the_right_answer(api: ApiHarness) -> None:
    report = await run_matrix(api)
    assert len(report.results) > 150  # the table really ran
    assert [f"{f.name}: {f.detail}" for f in report.failures] == []


async def test_the_matrix_notices_a_missing_tenant_filter(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that cannot fail proves nothing: remove the filter from one query and the matrix
    must complain about exactly the cells that depend on it."""
    from hastori_common.models import Device

    async def unscoped(session: Any, scope: Any, device_id: uuid.UUID) -> Device:
        device: Device | None = await session.get(Device, device_id)
        if device is None:
            raise not_found("Device not found")
        return device

    monkeypatch.setattr(measurements, "get_device", unscoped)
    report = await run_matrix(api)
    names = [f.name for f in report.failures]
    assert any(n.startswith("measurements Antalya as izmir_viewer") for n in names)
    assert any(n.startswith("measurements İzmir as antalya_admin") for n in names)
    assert all("measurements" in n for n in names)  # nothing else is affected


# -- structure: the filter cannot be forgotten --------------------------------------------------

QUERY_MODULES = [alarms, consumption, devices, measurements, rules, sites, users]
# functions that deliberately take no scope
NO_SCOPE = {"rule_out"}


def test_every_public_query_function_takes_the_scope() -> None:
    missing = []
    for module in QUERY_MODULES:
        for name, fn in inspect.getmembers(module, inspect.iscoroutinefunction):
            if name.startswith("_") or fn.__module__ != module.__name__ or name in NO_SCOPE:
                continue
            if "scope" not in inspect.signature(fn).parameters:
                missing.append(f"{module.__name__}.{name}")
    assert missing == []


def test_routers_do_not_query_the_database_themselves() -> None:
    """Routers call functions of `queries` (which take the scope); a SELECT written in a router
    would not carry the tenant filter. Sign-in is the one exception: nobody is signed in yet."""
    forbidden = re.compile(
        r"\bselect\(|\.execute\(|\.scalar\(|\.scalars\(|\bsession\.get\(|\btext\("
    )
    offenders = []
    for path in sorted((API_SRC / "routers").glob("*.py")):
        if path.name in {"auth.py", "health.py", "__init__.py"}:
            continue
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == []
    assert queries.__name__  # the package exists


def test_a_site_scope_of_nothing_sees_nothing() -> None:
    """`site_id IN ()` must be an empty set, not 'no filter'."""
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from hastori_common.models import Device

    sql = str(
        select(Device.id)
        .where(Device.site_id.in_(frozenset()))
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})  # type: ignore[no-untyped-call]
    )
    assert "1 != 1" in sql or "NULL" in sql or "false" in sql.lower()


async def test_a_user_with_no_sites_sees_no_data(api: ApiHarness) -> None:
    conn = await asyncpg.connect(api.dsn)
    await conn.execute("DELETE FROM user_sites")
    await conn.close()
    viewer = await api.signed_in("izmir_viewer")
    assert (await viewer.get("/sites")).json() == []
    assert (await viewer.get("/alarms")).json()["items"] == []
    izmir = (await (await api.signed_in("admin")).get("/sites")).json()
    site = next(s for s in izmir if s["name"] == "İzmir Fabrika")
    assert (await viewer.get(f"/sites/{site['id']}/devices")).status_code == 404


async def test_a_token_cannot_be_used_across_a_role_change(api: ApiHarness) -> None:
    """Demoting a site admin applies to the token they already hold."""
    admin = await api.signed_in("izmir_admin")
    sites_ = (await admin.get("/sites")).json()
    assert len(sites_) == 1
    rules_ = await admin.get("/alarm-rules")
    assert rules_.status_code == 200
    conn = await asyncpg.connect(api.dsn)
    await conn.execute("UPDATE users SET role = 'viewer' WHERE email LIKE 'izmir.admin%'")
    await conn.close()
    assert (await admin.get("/alarm-rules")).status_code == 403


async def test_error_responses_never_leak_internals(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    for path in (
        "/sites/%27%20OR%201=1--/devices",
        f"/devices/{uuid.uuid4()}/measurements?metric=temperature_c%27;DROP TABLE users;--",
        "/alarms?state=active%27%20OR%20%271%27=%271",
    ):
        r = await admin.get(path)
        assert r.status_code == 422, path
        assert "Traceback" not in r.text and "sqlalchemy" not in r.text.lower()
    conn = await asyncpg.connect(api.dsn)
    assert await conn.fetchval("SELECT count(*) FROM users") == 5  # still there
    await conn.close()


async def test_a_server_error_is_reported_without_details(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("secret database detail")

    monkeypatch.setattr(sites, "list_sites", boom)
    # the test client re-raises server errors by default; ask for the real response
    app_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
        base_url="http://test/api/v1",
    )
    login = await app_client.post(
        "/auth/login",
        json={
            "email": matrix.ACTORS[0] and "admin@demo.hastori.local",
            "password": "admin-test-password",
        },
    )
    r = await app_client.get(
        "/sites", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    await app_client.aclose()
    assert r.status_code == 500 and r.json() == {
        "error": {"code": "internal_error", "message": "Something went wrong"}
    }


PUBLIC = {
    ("POST", "/auth/login"),
    ("POST", "/auth/refresh"),
    ("POST", "/auth/logout"),
    ("GET", "/healthz"),
    ("GET", "/readyz"),
}


def test_every_route_is_covered_by_the_matrix(api_app_paths: set[tuple[str, str]]) -> None:
    """A new endpoint cannot skip the authorization matrix: each (method, path) of the OpenAPI
    document must be in the no-token list (and so in the table next to it), or be public."""
    covered = {(m, p.split("?")[0].replace(matrix.ZERO, "{}")) for m, p in matrix.UNAUTHENTICATED}
    missing = sorted(api_app_paths - covered - PUBLIC)
    assert missing == []


@pytest.fixture
def api_app_paths() -> set[tuple[str, str]]:
    from fakeredis import FakeAsyncRedis
    from sqlalchemy.ext.asyncio import create_async_engine

    from hastori_api.app import create_app
    from hastori_common.settings import Settings

    settings = Settings.model_validate({"jwt_secret": "t" * 40})
    app = create_app(
        settings, create_async_engine("postgresql+asyncpg://x@127.0.0.1/none"), FakeAsyncRedis()
    )
    paths: set[tuple[str, str]] = set()
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            paths.add((method.upper(), re.sub(r"\{[^}]+\}", "{}", path.removeprefix("/api/v1"))))
    return paths
