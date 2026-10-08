"""Findings of the day 2-3 review: what an attacker or a bad moment could do to the API."""

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from redis.asyncio import Redis

from conftest import DEMO_USERS, ApiHarness
from hastori_api import security
from hastori_api.errors import ApiError
from hastori_api.ratelimit import MAX_FAILURES_PER_IP, LoginLimiter
from hastori_api.times import as_utc
from hastori_api.tokens import RefreshStore

ADMIN_EMAIL, ADMIN_PW = DEMO_USERS["admin"]
USER = uuid.uuid4()


# -- sign-in: limits and pool -------------------------------------------------------------------


async def test_failures_from_one_address_block_it_whatever_the_e_mail(fake_redis: Redis) -> None:
    limiter = LoginLimiter(fake_redis)
    for i in range(MAX_FAILURES_PER_IP):
        await limiter.failed(f"user{i}@example.com", "203.0.113.7")
    assert await limiter.blocked_for("someone.new@example.com", "203.0.113.7") > 0
    # another address is not affected, and neither is the e-mail from elsewhere
    assert await limiter.blocked_for("someone.new@example.com", "203.0.113.8") == 0
    assert await limiter.blocked_for("user0@example.com", "203.0.113.8") == 0


async def test_a_flood_of_sign_ins_gets_a_fast_503_not_a_queue(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security, "_pending", security.MAX_PENDING_HASHES)
    r = await api.client().post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"
    assert r.json()["error"]["code"] == "unavailable"


async def test_the_pending_counter_returns_to_zero(api: ApiHarness) -> None:
    before = security._pending
    r = await api.client().post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
    assert r.status_code == 401
    assert security._pending == before


async def test_a_sign_in_does_not_hold_a_pooled_connection_while_it_hashes(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    real = security.verify_password

    async def spy(password: str, password_hash: str | None) -> bool:
        seen.append(api.engine.pool.checkedout())
        return await real(password, password_hash)

    monkeypatch.setattr("hastori_api.routers.auth.verify_password", spy)
    r = await api.client().post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW})
    assert r.status_code == 200
    assert seen == [0]


# -- refresh tokens ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_a_session_cannot_be_refreshed_for_ever(fake_redis: Redis) -> None:
    clock = Clock()
    store = RefreshStore(
        fake_redis, ttl_s=7 * 86400, grace_s=20, max_life_s=30 * 86400, clock=clock
    )
    token = await store.issue(USER)
    for _ in range(4):  # a refresh every six days keeps a plain 7-day TTL alive
        clock.now += 6 * 86400
        result = await store.rotate(token)
        assert result.status == "ok"
        token = result.token or ""
    # the family's records may not outlive its absolute end (30 days after the login)
    ttl = await fake_redis.ttl(f"rt:{store.digest(token)}")
    assert 0 < ttl <= 6 * 86400
    clock.now += 6 * 86400  # day 30 is passed: the next refresh is refused and the family goes
    assert (await store.rotate(token)).status == "missing"
    assert await fake_redis.keys("fam:*") == []


async def test_an_old_record_without_a_birth_time_still_rotates(fake_redis: Redis) -> None:
    clock = Clock()
    store = RefreshStore(fake_redis, ttl_s=7 * 86400, grace_s=20, clock=clock)
    token = await store.issue(USER)
    await fake_redis.hdel(f"rt:{store.digest(token)}", "born")  # issued before this field existed
    assert (await store.rotate(token)).status == "ok"


async def test_a_refresh_that_hits_a_database_error_still_delivers_the_new_cookie(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token is rotated before the user is looked up. A bare 503 would lose the new cookie
    and the next refresh with the old one would, after the grace window, revoke the session."""
    c = api.client()
    login = await c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW})
    old_cookie = c.cookies.get("refresh_token", domain="test.local")

    async def broken(*_: Any) -> None:
        raise ConnectionError("database is away")

    monkeypatch.setattr("hastori_api.routers.auth.load_scope", broken)
    r = await c.post("/auth/refresh")
    assert login.status_code == 200 and r.status_code == 503
    assert "refresh_token=" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert c.cookies.get("refresh_token", domain="test.local") != old_cookie
    monkeypatch.undo()
    assert (await c.post("/auth/refresh")).status_code == 200  # the client simply retries


# -- times ------------------------------------------------------------------------------------


def test_a_time_before_year_one_is_a_bad_request_not_a_crash() -> None:
    with pytest.raises(ApiError) as exc:
        as_utc(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=3))))
    assert exc.value.status == 422
    assert as_utc(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)


async def test_the_measurement_endpoint_answers_422_for_such_a_time(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    sites = (await admin.get("/sites")).json()
    devices = (await admin.get(f"/sites/{sites[0]['id']}/devices")).json()
    r = await admin.get(
        f"/devices/{devices[0]['id']}/measurements",
        params={"metric": "current_a", "from": "0001-01-01T00:00:00+03:00"},
    )
    assert r.status_code == 422


# -- users ------------------------------------------------------------------------------------


async def test_demoting_a_system_admin_needs_the_sites_they_keep(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    me = (await admin.get("/auth/me")).json()
    sites = [s["id"] for s in me["sites"]]
    other = await admin.post(
        "/users",
        json={
            "email": "second.admin@demo.hastori.local",
            "password": "a-long-enough-password",
            "role": "system_admin",
            "site_ids": [],
        },
    )
    assert other.status_code == 201, other.text
    refused = await admin.patch(f"/users/{me['id']}", json={"role": "viewer"})
    assert refused.status_code == 422
    assert "site_ids" in refused.json()["error"]["message"]
    done = await admin.patch(f"/users/{me['id']}", json={"role": "viewer", "site_ids": sites[:1]})
    assert done.status_code == 200 and done.json()["site_ids"] == sites[:1]
