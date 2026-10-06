"""Sign in, refresh and sign out through the real application (PostgreSQL, Redis)."""

import asyncio
import time
import uuid
from typing import Any

import jwt

from conftest import DEMO_USERS, ApiHarness
from hastori_api.security import issue_access_token

ADMIN_EMAIL, ADMIN_PW = DEMO_USERS["izmir_admin"]
# httpx files cookies of a dotless host (http://test) under this domain
COOKIE_DOMAIN = "test.local"


async def login(c: Any, email: str = ADMIN_EMAIL, password: str = ADMIN_PW) -> Any:
    return await c.post("/auth/login", json={"email": email, "password": password})


# -- login ------------------------------------------------------------------------------------


async def test_login_returns_an_access_token_and_sets_the_refresh_cookie(api: ApiHarness) -> None:
    r = await login(api.client())
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    assert "refresh" not in r.text.lower().replace("refresh_token=", "")  # not in the body
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 900

    cookie = r.headers["set-cookie"]
    assert cookie.startswith("refresh_token=")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/v1/auth" in cookie
    assert "Max-Age=604800" in cookie  # 7 days
    assert "Secure" not in cookie  # plain http on localhost; COOKIE_SECURE=true behind HTTPS


async def test_email_is_matched_without_regard_to_case_or_padding(api: ApiHarness) -> None:
    r = await login(api.client(), "  IZMIR.Admin@Demo.Hastori.LOCAL ")
    assert r.status_code == 200


async def test_a_wrong_password_and_an_unknown_user_look_the_same(api: ApiHarness) -> None:
    c = api.client()
    wrong = await login(c, ADMIN_EMAIL, "not-the-password")
    unknown = await login(c, "nobody@demo.hastori.local", "whatever")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert "set-cookie" not in wrong.headers


async def test_the_sixth_attempt_after_five_failures_is_refused_even_with_the_right_password(
    api: ApiHarness,
) -> None:
    c = api.client()
    for _ in range(5):
        assert (await login(c, ADMIN_EMAIL, "bad")).status_code == 401
    blocked = await login(c)  # the correct password
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["retry-after"]) <= 300
    assert blocked.json()["error"]["code"] == "too_many_requests"
    # another account from the same address is not affected
    assert (await login(c, *DEMO_USERS["admin"])).status_code == 200


async def test_a_successful_login_clears_the_failures(api: ApiHarness) -> None:
    c = api.client()
    for _ in range(4):
        await login(c, ADMIN_EMAIL, "bad")
    assert (await login(c)).status_code == 200
    for _ in range(4):
        assert (await login(c, ADMIN_EMAIL, "bad")).status_code == 401  # a fresh count


async def test_login_validates_its_input(api: ApiHarness) -> None:
    c = api.client()
    r = await c.post("/auth/login", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert {d["loc"][-1] for d in r.json()["error"]["details"]} == {"email", "password"}
    assert (
        await c.post("/auth/login", json={"email": "a@b.co", "password": ""})
    ).status_code == 422
    assert (await c.post("/auth/login", content=b"not json")).status_code == 422


# -- the bearer token -------------------------------------------------------------------------


async def test_me_describes_the_signed_in_user_and_their_sites(api: ApiHarness) -> None:
    viewer = await api.signed_in("izmir_viewer")
    me = (await viewer.get("/auth/me")).json()
    assert me["email"] == DEMO_USERS["izmir_viewer"][0] and me["role"] == "viewer"
    assert [s["name"] for s in me["sites"]] == ["İzmir Fabrika"]
    assert me["sites"][0]["timezone"] == "Europe/Istanbul"

    admin = await api.signed_in("admin")
    me = (await admin.get("/auth/me")).json()
    assert me["role"] == "system_admin"
    assert sorted(s["name"] for s in me["sites"]) == ["Antalya Otel", "İzmir Fabrika"]


async def test_a_request_without_a_valid_token_gets_401_in_the_error_format(
    api: ApiHarness,
) -> None:
    c = api.client()
    for headers in (
        {},
        {"Authorization": "Bearer garbage"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
    ):
        r = await c.get("/auth/me", headers=headers)
        assert r.status_code == 401, headers
        assert r.json()["error"]["code"] == "unauthorized"
        assert r.headers["www-authenticate"] == "Bearer"


async def test_an_expired_token_is_refused(api: ApiHarness) -> None:
    settings = api.app.state.settings
    user_id = uuid.uuid4()
    old = issue_access_token(settings, user_id, now=time.time() - 3600)
    r = await api.client().get("/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401 and "expired" in r.json()["error"]["message"]


async def test_a_valid_token_for_a_user_that_no_longer_exists_is_refused(api: ApiHarness) -> None:
    ghost = issue_access_token(api.app.state.settings, uuid.uuid4())
    r = await api.client().get("/auth/me", headers={"Authorization": f"Bearer {ghost}"})
    assert r.status_code == 401


async def test_a_changed_site_assignment_applies_to_the_existing_token(api: ApiHarness) -> None:
    """Sites are read from the database on every request, not baked into the token."""
    c = await api.signed_in("izmir_viewer")
    assert len((await c.get("/auth/me")).json()["sites"]) == 1
    await api.engine.dispose()  # drop pooled connections; the next request reconnects
    import asyncpg

    conn = await asyncpg.connect(api.dsn)
    await conn.execute(
        "DELETE FROM user_sites WHERE user_id = (SELECT id FROM users WHERE email = $1)",
        DEMO_USERS["izmir_viewer"][0],
    )
    await conn.close()
    assert (await c.get("/auth/me")).json()["sites"] == []


# -- refresh and logout -----------------------------------------------------------------------


async def test_refresh_gives_a_new_access_token_and_rotates_the_cookie(api: ApiHarness) -> None:
    c = api.client()
    first = await login(c)
    old_cookie = c.cookies["refresh_token"]
    r = await c.post("/auth/refresh")
    assert r.status_code == 200 and r.json()["access_token"] != first.json()["access_token"]
    assert c.cookies["refresh_token"] != old_cookie
    me = await c.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200


async def test_refresh_without_a_cookie_is_refused_and_clears_it(api: ApiHarness) -> None:
    r = await api.client().post("/auth/refresh")
    assert r.status_code == 401
    assert "refresh_token=;" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]


async def test_two_tabs_refreshing_at_once_both_get_a_token(api: ApiHarness) -> None:
    c = api.client()
    await login(c)
    cookie = c.cookies["refresh_token"]
    tabs = [api.client() for _ in range(3)]
    for t in tabs:
        t.cookies.set("refresh_token", cookie, domain=COOKIE_DOMAIN, path="/api/v1/auth")
    results = await asyncio.gather(*[t.post("/auth/refresh") for t in tabs])
    assert [r.status_code for r in results] == [200, 200, 200]
    assert len({t.cookies["refresh_token"] for t in tabs}) == 3  # each got its own new one


async def test_a_replayed_old_token_after_the_grace_window_ends_the_session(
    api: ApiHarness,
) -> None:
    owner, thief = api.client(), api.client()
    await login(owner)
    stolen = owner.cookies["refresh_token"]
    thief.cookies.set("refresh_token", stolen, domain=COOKIE_DOMAIN, path="/api/v1/auth")
    assert (await owner.post("/auth/refresh")).status_code == 200  # the owner refreshed
    newest = owner.cookies["refresh_token"]

    store = api.app.state.refresh_store
    real_clock = store.clock
    store.clock = lambda: real_clock() + 25  # the thief shows up after the 20 s grace window
    try:
        r = await thief.post("/auth/refresh")
    finally:
        store.clock = real_clock
    assert r.status_code == 401 and "already used" in r.json()["error"]["message"]
    # the owner's session is gone as well
    owner.cookies.set("refresh_token", newest, domain=COOKIE_DOMAIN, path="/api/v1/auth")
    assert (await owner.post("/auth/refresh")).status_code == 401


async def test_logout_ends_the_session_and_clears_the_cookie(api: ApiHarness) -> None:
    c = api.client()
    await login(c)
    r = await c.post("/auth/logout")
    assert r.status_code == 204 and "Max-Age=0" in r.headers["set-cookie"]
    assert (await c.post("/auth/refresh")).status_code == 401


async def test_logout_is_harmless_without_a_session(api: ApiHarness) -> None:
    assert (await api.client().post("/auth/logout")).status_code == 204


async def test_refresh_for_a_removed_user_is_refused(api: ApiHarness) -> None:
    import asyncpg

    c = api.client()
    await login(c, *DEMO_USERS["antalya_viewer"])
    conn = await asyncpg.connect(api.dsn)
    await conn.execute("DELETE FROM user_sites WHERE true")
    await conn.execute("DELETE FROM users WHERE email = $1", DEMO_USERS["antalya_viewer"][0])
    await conn.close()
    assert (await c.post("/auth/refresh")).status_code == 401


# -- the rest of the surface ------------------------------------------------------------------


async def test_unknown_routes_and_methods_use_the_error_format(api: ApiHarness) -> None:
    c = api.client()
    r = await c.get("/no/such/route")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = await c.get("/auth/login")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


async def test_swagger_and_the_openapi_document_are_served(api: ApiHarness) -> None:
    c = api.client()
    assert (await c.get("/docs")).status_code == 200
    spec = (await c.get("/openapi.json")).json()
    assert spec["info"]["title"] == "Hastori API"
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    assert {"/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/me"} <= set(spec["paths"])
    login_responses = spec["paths"]["/api/v1/auth/login"]["post"]["responses"]
    assert {"200", "401", "422", "429"} <= set(login_responses)


async def test_health_endpoints(api: ApiHarness) -> None:
    import httpx

    root = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test")
    try:
        assert (await root.get("/healthz")).json() == {"status": "ok"}
        ready = await root.get("/readyz")
        assert ready.status_code == 200 and ready.json() == {"db": True, "redis": True}
    finally:
        await root.aclose()
