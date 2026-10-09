"""A password change or a deactivation ends the user's sessions; an alarm keeps the name of who
acknowledged it (risk G3, G14)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from conftest import DEMO_USERS, ApiHarness

VIEWER_EMAIL, VIEWER_PW = DEMO_USERS["izmir_viewer"]
NEW_PW = "a-brand-new-password-1"


async def sql(api: ApiHarness, query: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(api.dsn)
    try:
        return list(await conn.fetch(query, *args))
    finally:
        await conn.close()


async def me(c: Any, token: str | None = None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return int((await c.get("/auth/me", headers=headers)).status_code)


async def login(c: Any, password: str = VIEWER_PW) -> str:
    r = await c.post("/auth/login", json={"email": VIEWER_EMAIL, "password": password})
    assert r.status_code == 200, r.text
    return str(r.json()["access_token"])


async def user_id(api: ApiHarness, email: str = VIEWER_EMAIL) -> str:
    (row,) = await sql(api, "SELECT id FROM users WHERE email = $1", email)
    return str(row["id"])


# -- own password change ------------------------------------------------------------------------


async def test_changing_your_password_ends_your_other_sessions_but_not_this_one(
    api: ApiHarness,
) -> None:
    laptop, phone = api.client(), api.client()
    laptop_token, phone_token = await login(laptop), await login(phone)
    assert await me(laptop, laptop_token) == await me(phone, phone_token) == 200

    r = await laptop.post(
        "/auth/password",
        json={"current_password": VIEWER_PW, "new_password": NEW_PW},
        headers={"Authorization": f"Bearer {laptop_token}"},
    )
    assert r.status_code == 200
    assert r.headers["set-cookie"].startswith("refresh_token=")
    assert await me(laptop, r.json()["access_token"]) == 200  # this device carries on

    assert await me(phone, phone_token) == 401  # the other one's access token is dead at once
    assert (await phone.post("/auth/refresh")).status_code == 401  # and so is its refresh cookie
    assert (await laptop.post("/auth/refresh")).status_code == 200  # this one still renews

    other = api.client()
    wrong = await other.post("/auth/login", json={"email": VIEWER_EMAIL, "password": VIEWER_PW})
    assert wrong.status_code == 401  # the old password is gone
    assert await login(other, NEW_PW)


async def test_a_wrong_current_password_changes_nothing(api: ApiHarness) -> None:
    c = api.client()
    token = await login(c)
    headers = {"Authorization": f"Bearer {token}"}
    r = await c.post(
        "/auth/password",
        json={"current_password": "not-my-password", "new_password": NEW_PW},
        headers=headers,
    )
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert await me(c, token) == 200
    assert await login(api.client(), VIEWER_PW)


async def test_a_short_new_password_is_refused(api: ApiHarness) -> None:
    c = api.client()
    token = await login(c)
    r = await c.post(
        "/auth/password",
        json={"current_password": VIEWER_PW, "new_password": "short"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


async def test_a_password_change_is_in_the_audit_log(api: ApiHarness) -> None:
    c = api.client()
    token = await login(c)
    await c.post(
        "/auth/password",
        json={"current_password": VIEWER_PW, "new_password": NEW_PW},
        headers={"Authorization": f"Bearer {token}"},
    )
    rows = await sql(api, "SELECT action, user_id FROM audit_log")
    assert [(r["action"], str(r["user_id"])) for r in rows] == [
        ("auth.password_change", await user_id(api))
    ]


# -- a system admin changes someone's password, or deactivates them -----------------------------


async def test_a_password_set_by_an_admin_ends_the_users_sessions(api: ApiHarness) -> None:
    viewer = api.client()
    token = await login(viewer)
    admin = await api.signed_in("admin")
    r = await admin.patch(f"/users/{await user_id(api)}", json={"password": NEW_PW})
    assert r.status_code == 200
    assert await me(viewer, token) == 401
    assert (await viewer.post("/auth/refresh")).status_code == 401
    assert await login(api.client(), NEW_PW)


async def test_a_deactivated_user_loses_every_session_and_cannot_sign_in(api: ApiHarness) -> None:
    viewer = api.client()
    token = await login(viewer)
    admin = await api.signed_in("admin")
    uid = await user_id(api)

    r = await admin.patch(f"/users/{uid}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert await me(viewer, token) == 401
    assert (await viewer.post("/auth/refresh")).status_code == 401

    again = await api.client().post(
        "/auth/login", json={"email": VIEWER_EMAIL, "password": VIEWER_PW}
    )
    wrong = await api.client().post(
        "/auth/login", json={"email": VIEWER_EMAIL, "password": "not-the-password"}
    )
    assert again.status_code == 401 and again.json() == wrong.json()  # nothing tells them apart

    r = await admin.patch(f"/users/{uid}", json={"is_active": True})
    assert r.json()["is_active"] is True
    assert await me(viewer, token) == 401  # the old token stays dead (the version moved on)
    assert await login(api.client())  # but a new sign-in works


async def test_the_demo_login_does_not_open_a_deactivated_account(api: ApiHarness) -> None:
    demo = api.variant(demo_login=True)
    try:
        admin = await api.signed_in("admin")
        await admin.patch(f"/users/{await user_id(api)}", json={"is_active": False})
        r = await demo.client().post("/auth/demo", json={"role": "viewer"})
        assert r.status_code == 404
    finally:
        await demo.close()


async def test_you_cannot_deactivate_yourself_or_the_last_active_system_admin(
    api: ApiHarness,
) -> None:
    admin = await api.signed_in("admin")
    me_id = await user_id(api, DEMO_USERS["admin"][0])
    r = await admin.patch(f"/users/{me_id}", json={"is_active": False})
    assert r.status_code == 409

    # a second system admin: now the first may be deactivated by the second, but not the last
    created = await admin.post(
        "/users",
        json={
            "email": "second.admin@demo.hastori.local",
            "password": "second-admin-password",
            "role": "system_admin",
        },
    )
    assert created.status_code == 201
    second = api.client()
    token = (
        await second.post(
            "/auth/login",
            json={"email": "second.admin@demo.hastori.local", "password": "second-admin-password"},
        )
    ).json()["access_token"]
    second.headers["Authorization"] = f"Bearer {token}"
    assert (await second.patch(f"/users/{me_id}", json={"is_active": False})).status_code == 200
    r = await second.patch(f"/users/{created.json()['id']}", json={"is_active": False})
    assert r.status_code == 409


# -- the acknowledger's name ----------------------------------------------------------------------


async def test_an_acknowledged_alarm_keeps_the_name_when_the_account_changes(
    api: ApiHarness,
) -> None:
    alarm_id = uuid.uuid4()
    (rule,) = await sql(
        api, "SELECT id, device_id FROM alarm_rules WHERE kind = 'threshold' LIMIT 1"
    )
    await sql(
        api,
        "INSERT INTO alarms (id, rule_id, device_id, state, opened_at) "
        "VALUES ($1, $2, $3, 'active', $4)",
        alarm_id,
        rule["id"],
        rule["device_id"],
        datetime.now(UTC) - timedelta(minutes=2),
    )
    admin = await api.signed_in("izmir_admin")
    r = await admin.post(f"/alarms/{alarm_id}/ack")
    assert r.status_code == 200
    assert r.json()["acked_by_label"] == "izmir.admin" and "@" not in r.text

    await sql(
        api,
        "UPDATE users SET email = 'renamed@demo.hastori.local' WHERE email = $1",
        DEMO_USERS["izmir_admin"][0],
    )
    detail = (await (await api.signed_in("izmir_viewer")).get(f"/alarms/{alarm_id}")).json()
    assert detail["acked_by_label"] == "izmir.admin"
    assert detail["timeline"][1]["by"] == "izmir.admin"
