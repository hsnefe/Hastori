"""What each endpoint does, beyond who may call it: data, calculations, validation, audit."""

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from conftest import DEMO_USERS, ApiHarness
from hastori_api.queries import consumption
from hastori_api.scope import load_scope
from hastori_common.events import channel
from hastori_common.seed_data import load_seed

SEED = load_seed()
IZMIR = SEED.site_by_key("izmir")
ANTALYA = SEED.site_by_key("antalya")
KOMP1 = SEED.device_by_key("izmir-komp-1")
KOMP2 = SEED.device_by_key("izmir-komp-2")
SOGUTMA = SEED.device_by_key("izmir-sogutma-1")
PANO = SEED.device_by_key("izmir-pano")
TEMP_RULE = next(r for r in SEED.alarm_rules if r.device == "izmir-komp-1")


async def sql(api: ApiHarness, query: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(api.dsn)
    try:
        rows: list[asyncpg.Record] = await conn.fetch(query, *args)
        return rows
    finally:
        await conn.close()


async def add_readings(
    api: ApiHarness,
    device: uuid.UUID,
    metric: str,
    start: datetime,
    count: int,
    step_s: float = 2.0,
    value: float = 70.0,
) -> None:
    rows = [
        (start + timedelta(seconds=i * step_s), device, metric, value + i) for i in range(count)
    ]
    conn = await asyncpg.connect(api.dsn)
    await conn.executemany(
        "INSERT INTO measurements (time, device_id, metric, value) VALUES ($1, $2, $3, $4)", rows
    )
    await conn.close()


# -- devices ----------------------------------------------------------------------------------


async def test_devices_come_with_their_latest_values_and_online_state(api: ApiHarness) -> None:
    now = datetime.now(UTC)
    for metric, v in (("temperature_c", 71.5), ("current_a", 60.0)):
        await add_readings(api, KOMP1.id, metric, now - timedelta(seconds=4), 1, value=v)
    await add_readings(api, KOMP1.id, "temperature_c", now - timedelta(minutes=3), 1, value=55)
    await add_readings(api, KOMP2.id, "temperature_c", now - timedelta(minutes=5), 1, value=66.0)

    c = await api.signed_in("izmir_viewer")
    r = await c.get(f"/sites/{IZMIR.id}/devices")
    assert r.status_code == 200
    by_key = {d["key"]: d for d in r.json()}
    assert sorted(by_key) == ["izmir-komp-1", "izmir-komp-2", "izmir-pano", "izmir-sogutma-1"]
    komp1 = by_key["izmir-komp-1"]
    assert komp1["online"] is True and komp1["type"] == "compressor"
    assert komp1["latest"]["temperature_c"]["value"] == 71.5  # the newest, not the 3 minute old
    assert komp1["latest"]["current_a"]["value"] == 60.0
    assert by_key["izmir-komp-2"]["online"] is False  # last reading 5 minutes ago
    assert by_key["izmir-komp-2"]["latest"]["temperature_c"]["value"] == 66.0
    assert (
        by_key["izmir-sogutma-1"]["latest"] == {} and by_key["izmir-sogutma-1"]["last_seen"] is None
    )


async def test_readings_older_than_ten_minutes_are_not_latest(api: ApiHarness) -> None:
    await add_readings(api, SOGUTMA.id, "current_a", datetime.now(UTC) - timedelta(minutes=11), 1)
    c = await api.signed_in("izmir_viewer")
    rows = {d["key"]: d for d in (await c.get(f"/sites/{IZMIR.id}/devices")).json()}
    assert rows["izmir-sogutma-1"]["latest"] == {}


# -- measurements -----------------------------------------------------------------------------


async def test_a_short_window_returns_raw_readings_in_time_order(api: ApiHarness) -> None:
    now = datetime.now(UTC)
    await add_readings(api, KOMP1.id, "temperature_c", now - timedelta(seconds=200), 100)
    c = await api.signed_in("izmir_viewer")
    r = await c.get(f"/devices/{KOMP1.id}/measurements", params={"metric": "temperature_c"})
    body = r.json()
    assert r.status_code == 200 and body["interval"] == "raw"  # auto, default 15 minutes
    times = [p["time"] for p in body["points"]]
    assert len(times) == 100 and times == sorted(times)
    assert body["points"][0]["min"] is None  # a raw reading has no spread
    assert body["points"][0]["value"] == 70.0 and body["points"][-1]["value"] == 169.0


async def test_a_longer_window_is_aggregated_per_minute(api: ApiHarness) -> None:
    start = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    await add_readings(api, KOMP1.id, "temperature_c", start, 60, step_s=2.0, value=60.0)  # 2 min
    c = await api.signed_in("izmir_viewer")
    r = await c.get(
        f"/devices/{KOMP1.id}/measurements",
        params={
            "metric": "temperature_c",
            "from": "2026-10-06T07:00:00Z",
            "to": "2026-10-06T11:00:00Z",
        },
    )
    body = r.json()
    assert body["interval"] == "1m" and len(body["points"]) == 2
    first = body["points"][0]
    assert first["time"].startswith("2026-10-06T08:00:00")
    assert first["min"] == 60.0 and first["max"] == 89.0 and first["value"] == 74.5  # 30 readings


async def test_a_long_window_is_aggregated_per_hour(api: ApiHarness) -> None:
    for stamp, value in (
        (datetime(2026, 9, 20, 8, 0, 5, tzinfo=UTC), 88.0),
        (datetime(2026, 9, 20, 9, 0, 10, tzinfo=UTC), 99.0),
        (datetime(2026, 9, 20, 9, 30, 20, tzinfo=UTC), 109.0),
    ):
        await add_readings(api, KOMP1.id, "temperature_c", stamp, 1, value=value)
    c = await api.signed_in("izmir_viewer")
    r = await c.get(
        f"/devices/{KOMP1.id}/measurements",
        params={
            "metric": "temperature_c",
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-10-01T00:00:00Z",
        },
    )
    body = r.json()
    assert body["interval"] == "1h"
    eight, nine = body["points"]
    assert eight["time"].startswith("2026-09-20T08:00:00") and eight["value"] == 88.0
    assert nine["time"].startswith("2026-09-20T09:00:00")
    assert (nine["min"], nine["max"], nine["value"]) == (99.0, 109.0, 104.0)


async def test_the_interval_can_be_chosen_and_a_time_without_offset_is_utc(api: ApiHarness) -> None:
    await add_readings(api, KOMP1.id, "current_a", datetime(2026, 10, 6, 8, 0, tzinfo=UTC), 30)
    c = await api.signed_in("izmir_viewer")
    r = await c.get(
        f"/devices/{KOMP1.id}/measurements",
        params={
            "metric": "current_a",
            "from": "2026-10-06T08:00:00",
            "to": "2026-10-06T09:00:00",
            "interval": "1m",
        },
    )
    assert r.status_code == 200 and r.json()["interval"] == "1m"
    assert r.json()["start"].startswith("2026-10-06T08:00:00") and len(r.json()["points"]) == 1


async def test_more_than_5000_points_are_refused_with_advice(api: ApiHarness) -> None:
    now = datetime.now(UTC)
    await add_readings(
        api, KOMP1.id, "temperature_c", now - timedelta(minutes=95), 5100, step_s=1.0
    )
    c = await api.signed_in("izmir_viewer")
    r = await c.get(
        f"/devices/{KOMP1.id}/measurements",
        params={
            "metric": "temperature_c",
            "from": (now - timedelta(minutes=100)).isoformat(),
            "to": now.isoformat(),
        },
    )
    assert r.status_code == 422 and "5000" in r.json()["error"]["message"]
    # asking for minutes instead works
    ok = await c.get(
        f"/devices/{KOMP1.id}/measurements",
        params={
            "metric": "temperature_c",
            "from": (now - timedelta(minutes=100)).isoformat(),
            "to": now.isoformat(),
            "interval": "1m",
        },
    )
    assert ok.status_code == 200 and len(ok.json()["points"]) < 5000


async def test_invalid_measurement_requests_are_422(api: ApiHarness) -> None:
    c = await api.signed_in("izmir_viewer")
    base = f"/devices/{KOMP1.id}/measurements"
    now = datetime.now(UTC)
    cases = [
        ({}, "metric is required"),
        ({"metric": "voltage"}, "unknown metric"),
        ({"metric": "current_a", "interval": "5m"}, "unknown interval"),
        (
            {
                "metric": "current_a",
                "from": now.isoformat(),
                "to": (now - timedelta(hours=1)).isoformat(),
            },
            "from after to",
        ),
        (
            {
                "metric": "current_a",
                "from": (now - timedelta(days=91)).isoformat(),
                "to": now.isoformat(),
            },
            "longer than 90 days",
        ),
        ({"metric": "current_a", "from": "yesterday"}, "not a time"),
    ]
    for params, why in cases:
        r = await c.get(base, params=params)
        assert r.status_code == 422, why
        assert r.json()["error"]["code"] == "validation_error"


# -- daily consumption ------------------------------------------------------------------------


async def put_panel_minutes(
    api: ApiHarness, start: datetime, end: datetime, kw: float = 100.0, kvar: float = 14.0
) -> None:
    conn = await asyncpg.connect(api.dsn)
    for metric, value in (("active_power_kw", kw), ("reactive_power_kvar", kvar)):
        await conn.execute(
            "INSERT INTO measurements (time, device_id, metric, value) "
            "SELECT g, $1, $2, $3 FROM generate_series($4::timestamptz, $5::timestamptz, "
            "interval '1 minute') g",
            PANO.id,
            metric,
            value,
            start,
            end,
        )
    await conn.close()


async def scope_for(api: ApiHarness, who: str) -> Any:
    async with api.app.state.sessionmaker() as session:
        row = (
            await session.execute(
                __import__("sqlalchemy").text("SELECT id FROM users WHERE email = :e"),
                {"e": DEMO_USERS[who][0]},
            )
        ).first()
        return await load_scope(session, row[0])


async def daily(
    api: ApiHarness, who: str, now: datetime, days: int = 3, site: uuid.UUID = IZMIR.id
) -> Any:
    scope = await scope_for(api, who)
    async with api.app.state.sessionmaker() as session:
        return await consumption.daily(session, scope, site, days, now=now)


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)  # 15:00 in Istanbul (UTC+3)


async def test_daily_energy_is_the_integral_of_power_in_the_site_time_zone(api: ApiHarness) -> None:
    # local days of 2026-10-05 and 10-06 (Istanbul midnight = 21:00 UTC the evening before)
    await put_panel_minutes(
        api, datetime(2026, 10, 4, 21, 0, tzinfo=UTC), NOW - timedelta(minutes=1)
    )
    out = await daily(api, "izmir_viewer", NOW)
    assert out.timezone == "Europe/Istanbul"
    assert [d.date for d in out.days] == [date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)]
    empty, yesterday, today = out.days
    assert empty.kwh == 0 and empty.coverage == 0 and empty.reactive_ratio is None
    assert yesterday.kwh == 2400.0  # 100 kW for 24 hours
    assert yesterday.coverage == 1.0
    assert today.kwh == 1500.0 and today.coverage == 1.0  # 15 hours so far, all of them present
    assert yesterday.reactive_ratio == 0.14 and yesterday.reactive_kvarh == 336.0


async def test_a_hole_in_the_data_lowers_coverage_instead_of_hiding(api: ApiHarness) -> None:
    await put_panel_minutes(
        api, datetime(2026, 10, 4, 21, 0, tzinfo=UTC), datetime(2026, 10, 5, 7, 59, tzinfo=UTC)
    )
    await put_panel_minutes(
        api, datetime(2026, 10, 5, 9, 0, tzinfo=UTC), NOW - timedelta(minutes=1)
    )  # an hour missing
    day = (await daily(api, "izmir_viewer", NOW)).days[1]
    assert day.kwh == 2300.0 and day.coverage == round(1380 / 1440, 4)


async def test_a_reading_belongs_to_the_local_day_it_happened_in(api: ApiHarness) -> None:
    for t in (
        datetime(2026, 10, 5, 20, 59, tzinfo=UTC),  # 23:59 on the 5th in Istanbul
        datetime(2026, 10, 5, 21, 0, tzinfo=UTC),  # 00:00 on the 6th
    ):
        await put_panel_minutes(api, t, t)
    out = await daily(api, "izmir_viewer", NOW)
    by_date = {d.date: d for d in out.days}
    assert round(by_date[date(2026, 10, 5)].kwh, 3) == round(100 / 60, 3)
    assert round(by_date[date(2026, 10, 6)].kwh, 3) == round(100 / 60, 3)


async def test_the_day_follows_the_time_zone_of_the_site(api: ApiHarness) -> None:
    await sql(api, "UPDATE sites SET timezone = 'America/Los_Angeles' WHERE id = $1", IZMIR.id)
    t = datetime(2026, 10, 6, 6, 59, tzinfo=UTC)  # 23:59 on the 5th in Los Angeles (UTC-7)
    await put_panel_minutes(api, t, t + timedelta(minutes=1))
    out = await daily(api, "izmir_viewer", NOW)
    by_date = {d.date: d for d in out.days}
    assert out.timezone == "America/Los_Angeles"
    assert by_date[date(2026, 10, 5)].kwh > 0 and by_date[date(2026, 10, 6)].kwh > 0


def test_a_day_is_not_always_24_hours() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    after = datetime(2026, 11, 1, tzinfo=UTC)
    assert consumption.minutes_in_day(date(2026, 10, 25), berlin, after) == 25 * 60  # clocks back
    assert consumption.minutes_in_day(date(2026, 3, 29), berlin, after) == 23 * 60  # clocks forward
    assert consumption.minutes_in_day(date(2026, 10, 6), berlin, after) == 24 * 60
    now = datetime(2026, 10, 6, 10, 30, tzinfo=UTC)  # 12:30 in Berlin
    assert consumption.minutes_in_day(date(2026, 10, 6), berlin, now) == 12 * 60 + 30
    assert consumption.minutes_in_day(date(2026, 10, 7), berlin, now) == 0  # not begun


async def test_the_consumption_of_one_site_never_includes_another(api: ApiHarness) -> None:
    await put_panel_minutes(
        api, datetime(2026, 10, 5, 21, 0, tzinfo=UTC), NOW - timedelta(minutes=1)
    )
    antalya = await daily(api, "antalya_viewer", NOW, site=ANTALYA.id)
    assert all(d.kwh == 0 for d in antalya.days)


async def test_a_site_without_a_main_panel_has_no_consumption(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    site = (await admin.post("/sites", json={"name": "Boş Depo"})).json()
    r = await admin.get(f"/sites/{site['id']}/consumption/daily")
    assert r.status_code == 200 and r.json()["days"] == []


async def test_consumption_parameters_are_validated(api: ApiHarness) -> None:
    c = await api.signed_in("izmir_viewer")
    base = f"/sites/{IZMIR.id}/consumption/daily"
    assert (await c.get(base, params={"days": 0})).status_code == 422
    assert (await c.get(base, params={"days": 91})).status_code == 422
    r = await c.get(base, params={"days": 2})
    assert r.status_code == 200 and len(r.json()["days"]) == 2


# -- alarms -----------------------------------------------------------------------------------


async def make_alarm(
    api: ApiHarness,
    state: str,
    minutes_ago: int,
    rule: Any = TEMP_RULE,
    device: Any = KOMP1,
    acked_by: str | None = None,
) -> str:
    alarm_id = uuid.uuid4()
    opened = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    await sql(
        api,
        "INSERT INTO alarms (id, rule_id, device_id, state, opened_at, acked_at, acked_by, "
        "cleared_at, peak_value) "
        "VALUES ($1, $2, $3, $4, $5, $6, (SELECT id FROM users WHERE email = $7), $8, 88.5)",
        alarm_id,
        rule.id,
        device.id,
        state,
        opened,
        opened + timedelta(minutes=1)
        if state in ("acknowledged", "cleared") and acked_by
        else None,
        acked_by,
        opened + timedelta(minutes=5) if state == "cleared" else None,
    )
    return str(alarm_id)


async def test_alarms_are_listed_newest_first_with_filters_and_paging(api: ApiHarness) -> None:
    ids = [
        await make_alarm(api, "cleared", 300),
        await make_alarm(api, "cleared", 200),
        await make_alarm(api, "active", 10),
    ]
    c = await api.signed_in("izmir_viewer")
    r = (await c.get("/alarms")).json()
    assert r["total"] == 3 and r["page"] == 1 and r["size"] == 20
    assert [a["id"] for a in r["items"]] == [ids[2], ids[1], ids[0]]
    first = r["items"][0]
    assert first["site_name"] == "İzmir Fabrika" and first["device_name"] == "Kompresör-1"
    assert first["severity"] == "critical" and first["peak_value"] == 88.5
    assert [
        a["id"] for a in (await c.get("/alarms", params={"state": "active"})).json()["items"]
    ] == [ids[2]]
    page2 = (await c.get("/alarms", params={"size": 2, "page": 2})).json()
    assert [a["id"] for a in page2["items"]] == [ids[0]] and page2["total"] == 3
    since = (datetime.now(UTC) - timedelta(minutes=250)).isoformat()
    assert len((await c.get("/alarms", params={"from": since})).json()["items"]) == 2
    assert (await c.get("/alarms", params={"size": 101})).status_code == 422
    assert (await c.get("/alarms", params={"state": "open"})).status_code == 422


async def test_an_alarm_comes_with_its_timeline_and_rule(api: ApiHarness) -> None:
    alarm = await make_alarm(api, "cleared", 60, acked_by=DEMO_USERS["izmir_admin"][0])
    c = await api.signed_in("izmir_viewer")
    body = (await c.get(f"/alarms/{alarm}")).json()
    assert [e["event"] for e in body["timeline"]] == ["opened", "acknowledged", "cleared"]
    assert body["timeline"][1]["by"] == DEMO_USERS["izmir_admin"][0]
    assert body["rule"]["threshold"] == 80 and body["rule"]["clear_threshold"] == 75
    assert body["acked_by"] is not None and body["cleared_at"] is not None


async def test_acknowledging_records_who_and_when_and_announces_it(api: ApiHarness) -> None:
    alarm = await make_alarm(api, "active", 3)
    pubsub = api.redis.pubsub()
    await pubsub.subscribe(channel(IZMIR.id))
    await pubsub.get_message(timeout=0.1)
    admin = await api.signed_in("izmir_admin")

    r = await admin.post(f"/alarms/{alarm}/ack")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "acknowledged" and body["acked_at"] is not None and body["acked_by"]

    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    await pubsub.aclose()
    event = json.loads(message["data"])
    assert event["type"] == "alarm.acknowledged" and event["alarm_id"] == alarm
    assert event["site_id"] == str(IZMIR.id) and event["state"] == "acknowledged"

    (audit,) = await sql(api, "SELECT user_id, action, entity, entity_id FROM audit_log")
    assert (audit["action"], audit["entity"], audit["entity_id"]) == (
        "alarm.acknowledge",
        "alarm",
        alarm,
    )
    assert str(audit["user_id"]) == body["acked_by"]


async def test_an_alarm_can_only_be_acknowledged_once_and_only_while_active(
    api: ApiHarness,
) -> None:
    alarm = await make_alarm(api, "active", 3)
    cleared = await make_alarm(api, "cleared", 90)
    admin = await api.signed_in("izmir_admin")
    assert (await admin.post(f"/alarms/{alarm}/ack")).status_code == 200
    again = await admin.post(f"/alarms/{alarm}/ack")
    assert again.status_code == 409 and again.json()["error"]["code"] == "conflict"
    assert (await admin.post(f"/alarms/{cleared}/ack")).status_code == 409
    assert len(await sql(api, "SELECT 1 FROM audit_log")) == 1  # the refused ones left no trace


async def test_two_people_acknowledging_at_once_one_wins(api: ApiHarness) -> None:
    alarm = await make_alarm(api, "active", 3)
    a, b = await api.signed_in("izmir_admin"), await api.signed_in("admin")
    results = await asyncio.gather(a.post(f"/alarms/{alarm}/ack"), b.post(f"/alarms/{alarm}/ack"))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(await sql(api, "SELECT 1 FROM audit_log")) == 1


async def test_an_alarm_closed_by_the_alarm_service_cannot_be_acknowledged(api: ApiHarness) -> None:
    alarm = await make_alarm(api, "active", 3)
    admin = await api.signed_in("izmir_admin")
    await sql(
        api,
        "UPDATE alarms SET state = 'cleared', cleared_at = now() WHERE id = $1",
        uuid.UUID(alarm),
    )
    assert (await admin.post(f"/alarms/{alarm}/ack")).status_code == 409
    state = (await sql(api, "SELECT state FROM alarms"))[0]["state"]
    assert state == "cleared"  # the acknowledgement did not overwrite the closure


# -- alarm rules ------------------------------------------------------------------------------

RULE = {
    "name": "Kompresör-2 yüksek sıcaklık",
    "metric": "temperature_c",
    "operator": ">",
    "threshold": 85,
    "clear_threshold": 80,
    "duration_s": 20,
    "severity": "warning",
}


async def test_rules_are_listed_for_the_sites_of_the_admin(api: ApiHarness) -> None:
    izmir = await api.signed_in("izmir_admin")
    r = (await izmir.get("/alarm-rules")).json()
    assert r["total"] == 3 and {x["site_id"] for x in r["items"]} == {str(IZMIR.id)}
    admin = await api.signed_in("admin")
    assert (await admin.get("/alarm-rules")).json()["total"] == 4
    only = (await admin.get("/alarm-rules", params={"device_id": str(KOMP1.id)})).json()
    assert [x["name"] for x in only["items"]] == [TEMP_RULE.name]
    assert (await admin.get("/alarm-rules", params={"enabled": "false"})).json()["total"] == 0
    assert (await izmir.get("/alarm-rules", params={"site_id": str(ANTALYA.id)})).status_code == 404


async def test_a_rule_can_be_created_changed_and_disabled(api: ApiHarness) -> None:
    c = await api.signed_in("izmir_admin")
    created = await c.post("/alarm-rules", json={**RULE, "device_id": str(KOMP2.id)})
    assert created.status_code == 201
    rule = created.json()
    assert (
        rule["enabled"] is True and rule["kind"] == "threshold" and rule["site_id"] == str(IZMIR.id)
    )
    assert (await c.get(f"/alarm-rules/{rule['id']}")).json() == rule

    changed = await c.put(
        f"/alarm-rules/{rule['id']}", json={**RULE, "threshold": 90, "clear_threshold": 85}
    )
    assert changed.status_code == 200 and changed.json()["threshold"] == 90

    assert (await c.delete(f"/alarm-rules/{rule['id']}")).status_code == 204
    assert (await c.get(f"/alarm-rules/{rule['id']}")).json()["enabled"] is False
    assert (await c.delete(f"/alarm-rules/{rule['id']}")).status_code == 204  # idempotent

    actions = [r["action"] for r in await sql(api, "SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["rule.create", "rule.update", "rule.disable"]  # the repeat changed nothing
    detail = (await sql(api, "SELECT detail FROM audit_log WHERE action = 'rule.update'"))[0][
        "detail"
    ]
    assert json.loads(detail)["threshold"] == 90


async def test_only_one_enabled_rule_per_device_metric_and_kind(api: ApiHarness) -> None:
    c = await api.signed_in("izmir_admin")
    first = (await c.post("/alarm-rules", json={**RULE, "device_id": str(KOMP2.id)})).json()
    second = await c.post(
        "/alarm-rules", json={**RULE, "device_id": str(KOMP2.id), "severity": "critical"}
    )
    assert second.status_code == 409 and "already" in second.json()["error"]["message"]
    off = await c.post("/alarm-rules", json={**RULE, "device_id": str(KOMP2.id), "enabled": False})
    assert off.status_code == 201  # a disabled one may sit next to it
    # turning it on while the first is enabled is refused, once the first is off it works
    again = await c.put(f"/alarm-rules/{off.json()['id']}", json={**RULE, "enabled": True})
    assert again.status_code == 409
    await c.delete(f"/alarm-rules/{first['id']}")
    assert (
        await c.put(f"/alarm-rules/{off.json()['id']}", json={**RULE, "enabled": True})
    ).status_code == 200


async def test_rule_validation(api: ApiHarness) -> None:
    c = await api.signed_in("izmir_admin")
    dev = {"device_id": str(KOMP2.id)}

    async def refused(body: dict[str, Any], why: str) -> None:
        r = await c.post("/alarm-rules", json={**RULE, **dev, **body})
        assert r.status_code == 422, why
        assert r.json()["error"]["code"] == "validation_error", why

    await refused({"clear_threshold": 86}, "clear threshold above a '>' threshold")
    await refused(
        {"operator": "<", "threshold": 5, "clear_threshold": 4}, "clear below a '<' threshold"
    )
    await refused({"clear_threshold": None}, "no hysteresis")
    await refused({"duration_s": -1}, "negative duration")
    await refused({"metric": "voltage"}, "unknown metric")
    await refused({"severity": "info"}, "unknown severity")
    await refused({"name": ""}, "empty name")
    await refused({"window_s": 600}, "a window on a threshold rule")
    await refused({"kind": "reactive_ratio"}, "a ratio rule without a window")
    await refused(
        {
            "kind": "reactive_ratio",
            "window_s": 30,
            "metric": "reactive_power_kvar",
            "threshold": 0.2,
            "clear_threshold": 0.1,
        },
        "window too short",
    )
    await refused(
        {
            "kind": "reactive_ratio",
            "window_s": 600,
            "metric": "current_a",
            "threshold": 0.2,
            "clear_threshold": 0.1,
        },
        "ratio on the wrong metric",
    )
    await refused(
        {
            "kind": "reactive_ratio",
            "window_s": 600,
            "metric": "reactive_power_kvar",
            "operator": "<",
            "threshold": 0.1,
            "clear_threshold": 0.2,
        },
        "ratio with '<'",
    )
    # a compressor measures one machine, not the site: no ratio rule there
    ratio = {
        "kind": "reactive_ratio",
        "window_s": 600,
        "metric": "reactive_power_kvar",
        "threshold": 0.2,
        "clear_threshold": 0.1,
    }
    r = await c.post("/alarm-rules", json={**RULE, **dev, **ratio})
    assert r.status_code == 422 and "energy analyzer" in r.json()["error"]["message"]
    # infinity is not a threshold
    bad = json.dumps({**RULE, **dev}).replace('"threshold": 85', '"threshold": 1e999')
    assert (
        await c.post("/alarm-rules", content=bad, headers={"Content-Type": "application/json"})
    ).status_code == 422
    assert await sql(api, "SELECT 1 FROM audit_log") == []  # nothing refused was recorded


async def test_a_ratio_rule_on_the_main_panel_is_accepted(api: ApiHarness) -> None:
    await sql(api, "UPDATE alarm_rules SET enabled = false WHERE kind = 'reactive_ratio'")
    c = await api.signed_in("izmir_admin")
    body = {
        "device_id": str(PANO.id), "name": "Pano oran", "kind": "reactive_ratio", "window_s": 900,
        "metric": "reactive_power_kvar", "operator": ">", "threshold": 0.2, "clear_threshold": 0.18,
        "duration_s": 0, "severity": "warning",
    }  # fmt: skip
    r = await c.post("/alarm-rules", json=body)
    assert r.status_code == 201 and r.json()["window_s"] == 900


async def test_a_rule_change_wakes_the_alarm_service(api: ApiHarness) -> None:
    listener = await asyncpg.connect(api.dsn)
    seen: list[str] = []
    await listener.add_listener("alarm_rules_changed", lambda *a: seen.append(a[3]))
    c = await api.signed_in("izmir_admin")
    rule = (await c.post("/alarm-rules", json={**RULE, "device_id": str(KOMP2.id)})).json()
    await c.delete(f"/alarm-rules/{rule['id']}")
    for _ in range(50):
        if len(seen) >= 2:
            break
        await asyncio.sleep(0.05)
    await listener.close()
    assert seen == [rule["id"], rule["id"]]


# -- users ------------------------------------------------------------------------------------


async def test_users_are_listed_with_their_sites(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    r = (await admin.get("/users", params={"size": 2})).json()
    assert r["total"] == 5 and len(r["items"]) == 2 and r["page"] == 1
    everyone = (await admin.get("/users")).json()["items"]
    emails = [u["email"] for u in everyone]
    assert emails == sorted(emails, key=str.lower)
    viewer = next(u for u in everyone if u["email"].startswith("izmir.izleyici"))
    assert viewer["role"] == "viewer" and viewer["site_ids"] == [str(IZMIR.id)]
    assert all("password" not in json.dumps(u) for u in everyone)


async def test_a_created_user_can_sign_in_and_sees_only_their_sites(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    r = await admin.post(
        "/users",
        json={
            "email": "Yeni.Kisi@demo.hastori.local",
            "password": "a-long-enough-pw",
            "role": "viewer",
            "site_ids": [str(ANTALYA.id)],
        },
    )
    assert r.status_code == 201
    assert r.json()["email"] == "Yeni.Kisi@demo.hastori.local" and r.json()["site_ids"] == [
        str(ANTALYA.id)
    ]
    assert "password" not in r.text
    c = api.client()
    login = await c.post(
        "/auth/login",
        json={"email": "yeni.kisi@demo.hastori.local", "password": "a-long-enough-pw"},
    )
    assert login.status_code == 200
    c.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    assert [s["name"] for s in (await c.get("/sites")).json()] == ["Antalya Otel"]
    (row,) = await sql(api, "SELECT detail FROM audit_log WHERE action = 'user.create'")
    assert "a-long-enough-pw" not in row["detail"] and "password" not in row["detail"]
    hash_ = (
        await sql(
            api,
            "SELECT password_hash FROM users WHERE lower(email) = 'yeni.kisi@demo.hastori.local'",
        )
    )[0][0]
    assert hash_.startswith("$argon2") and "a-long-enough-pw" not in hash_


async def test_user_creation_is_validated(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    ok = {"email": "x@demo.hastori.local", "password": "a-long-enough-pw", "role": "viewer"}
    assert (await admin.post("/users", json={**ok, "password": "short"})).status_code == 422
    assert (await admin.post("/users", json={**ok, "email": "not-an-email"})).status_code == 422
    assert (await admin.post("/users", json={**ok, "role": "root"})).status_code == 422
    unknown = await admin.post("/users", json={**ok, "site_ids": [str(uuid.uuid4())]})
    assert unknown.status_code == 422 and "Unknown sites" in unknown.json()["error"]["message"]
    dupe = await admin.post("/users", json={**ok, "email": DEMO_USERS["izmir_admin"][0].upper()})
    assert dupe.status_code == 409
    assert len(await sql(api, "SELECT 1 FROM users")) == 5


async def test_changing_a_users_role_sites_and_password(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    users = (await admin.get("/users")).json()["items"]
    viewer = next(u for u in users if u["email"].startswith("izmir.izleyici"))

    r = await admin.patch(
        f"/users/{viewer['id']}",
        json={"role": "site_admin", "site_ids": [str(IZMIR.id), str(ANTALYA.id)]},
    )
    assert (
        r.status_code == 200 and r.json()["role"] == "site_admin" and len(r.json()["site_ids"]) == 2
    )

    await admin.patch(f"/users/{viewer['id']}", json={"password": "a-brand-new-password"})
    c = api.client()
    old = await c.post(
        "/auth/login", json={"email": viewer["email"], "password": DEMO_USERS["izmir_viewer"][1]}
    )
    new = await c.post(
        "/auth/login", json={"email": viewer["email"], "password": "a-brand-new-password"}
    )
    assert old.status_code == 401 and new.status_code == 200

    promoted = await admin.patch(f"/users/{viewer['id']}", json={"role": "system_admin"})
    assert promoted.json()["site_ids"] == []  # a system admin sees every site anyway
    detail = (
        await sql(
            api,
            "SELECT detail FROM audit_log WHERE action = 'user.update' ORDER BY id DESC LIMIT 1",
        )
    )[0]["detail"]
    assert "a-brand-new-password" not in detail

    assert (await admin.patch(f"/users/{uuid.uuid4()}", json={"role": "viewer"})).status_code == 404


async def test_the_last_system_admin_cannot_be_demoted(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    me = (await admin.get("/auth/me")).json()
    r = await admin.patch(f"/users/{me['id']}", json={"role": "viewer"})
    assert r.status_code == 409
    assert (await admin.get("/auth/me")).json()["role"] == "system_admin"


# -- sites ------------------------------------------------------------------------------------


async def test_sites_can_be_added_and_changed_by_the_system_admin(api: ApiHarness) -> None:
    admin = await api.signed_in("admin")
    created = await admin.post("/sites", json={"name": "Bursa Depo", "city": "Bursa"})
    assert created.status_code == 201 and created.json()["timezone"] == "Europe/Istanbul"
    names = [s["name"] for s in (await admin.get("/sites")).json()]
    assert "Bursa Depo" in names
    izmir_admin = await api.signed_in("izmir_admin")
    assert "Bursa Depo" not in [s["name"] for s in (await izmir_admin.get("/sites")).json()]

    site_id = created.json()["id"]
    patched = await admin.patch(
        f"/sites/{site_id}", json={"timezone": "Europe/Berlin", "city": None}
    )
    assert patched.json()["timezone"] == "Europe/Berlin" and patched.json()["city"] is None
    assert (await admin.post("/sites", json={"name": "Bursa Depo"})).status_code == 409
    assert (
        await admin.patch(f"/sites/{site_id}", json={"name": "İzmir Fabrika"})
    ).status_code == 409
    assert (
        await admin.post("/sites", json={"name": "X", "timezone": "Mars/Olympus"})
    ).status_code == 422
    assert (await admin.patch(f"/sites/{uuid.uuid4()}", json={"city": "x"})).status_code == 404
    actions = [r["action"] for r in await sql(api, "SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["site.create", "site.update"]
