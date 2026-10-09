"""Passwordless demo sign-in: on only when the operator asks, viewer or site admin only."""

from typing import Any

import pytest

from conftest import DEMO_USERS, ApiHarness


@pytest.fixture
async def demo(api: ApiHarness) -> Any:
    h = api.variant(demo_login=True)
    try:
        yield h
    finally:
        await h.close()


async def test_it_is_off_by_default(api: ApiHarness) -> None:
    c = api.client()
    assert (await c.get("/auth/demo")).json() == {"enabled": False}
    r = await c.post("/auth/demo", json={"role": "viewer"})
    assert r.status_code == 404 and "set-cookie" not in r.headers


async def test_the_viewer_button_signs_in_the_izmir_viewer(demo: ApiHarness) -> None:
    c = demo.client()
    assert (await c.get("/auth/demo")).json() == {"enabled": True}
    r = await c.post("/auth/demo", json={"role": "viewer"})
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"
    assert r.headers["set-cookie"].startswith("refresh_token=")
    me = await c.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.json()["email"] == DEMO_USERS["izmir_viewer"][0]
    assert me.json()["role"] == "viewer"


async def test_the_site_admin_button_signs_in_the_izmir_site_admin(demo: ApiHarness) -> None:
    c = demo.client()
    r = await c.post("/auth/demo", json={"role": "site_admin"})
    me = await c.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.json()["email"] == DEMO_USERS["izmir_admin"][0]
    assert me.json()["role"] == "site_admin"
    assert [s["name"] for s in me.json()["sites"]] == ["İzmir Fabrika"]


async def test_the_session_it_opens_refreshes_like_any_other(demo: ApiHarness) -> None:
    c = demo.client()
    await c.post("/auth/demo", json={"role": "viewer"})
    assert (await c.post("/auth/refresh")).status_code == 200


@pytest.mark.parametrize("role", ["system_admin", "admin", "", None])
async def test_the_system_admin_cannot_be_asked_for(demo: ApiHarness, role: Any) -> None:
    r = await demo.client().post("/auth/demo", json={"role": role})
    assert r.status_code == 422 and "set-cookie" not in r.headers


async def test_a_misconfigured_email_never_opens_a_back_door(api: ApiHarness) -> None:
    """DEMO_VIEWER_EMAIL pointing at the system admin: the role check refuses it."""
    h = api.variant(demo_login=True, demo_viewer_email=DEMO_USERS["admin"][0])
    try:
        r = await h.client().post("/auth/demo", json={"role": "viewer"})
        assert r.status_code == 404 and "set-cookie" not in r.headers
    finally:
        await h.close()
