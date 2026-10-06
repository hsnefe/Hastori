"""Database access of the alarm service. Every write is conditional, so a message that is
processed twice (redelivery, a restart) cannot create a second alarm or close one twice."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from hastori_alarm.engine import Rule, Sample

OPEN_STATES = ("active", "acknowledged")

RULES_SQL = """
SELECT r.id, r.device_id, r.name, r.kind, r.metric, r.operator, r.threshold,
       r.clear_threshold, r.duration_s, r.window_s, r.severity
FROM alarm_rules r JOIN devices d ON d.id = r.device_id
WHERE r.enabled AND d.is_active
"""
DEVICES_SQL = "SELECT id, site_id FROM devices"
OPEN_ALARMS_SQL = """
SELECT a.id, a.rule_id, a.device_id, a.opened_at, a.peak_value, r.name AS rule_name,
       r.severity, d.site_id
FROM alarms a
JOIN alarm_rules r ON r.id = a.rule_id
JOIN devices d ON d.id = a.device_id
WHERE a.state IN ('active', 'acknowledged')
"""
WATERMARKS_SQL = """
SELECT rule_id, max(coalesce(cleared_at, opened_at)) AS last_event FROM alarms GROUP BY rule_id
"""
# `ON CONFLICT ... WHERE` infers the partial unique index: one open alarm per rule.
OPEN_SQL = """
INSERT INTO alarms (id, rule_id, device_id, state, opened_at, peak_value)
VALUES ($1, $2, $3, 'active', $4, $5)
ON CONFLICT (rule_id) WHERE state IN ('active', 'acknowledged') DO NOTHING
RETURNING id
"""
CLEAR_SQL = """
UPDATE alarms SET state = 'cleared', cleared_at = $2, peak_value = $3
WHERE rule_id = $1 AND state IN ('active', 'acknowledged')
RETURNING id
"""
SAMPLES_SQL = """
SELECT time, device_id, metric, value FROM measurements
WHERE device_id = ANY($1::uuid[]) AND time >= $2
ORDER BY time, device_id
"""


@dataclass(frozen=True)
class Catalog:
    rules: list[Rule]
    site_of_device: dict[uuid.UUID, uuid.UUID]


@dataclass(frozen=True)
class OpenAlarm:
    id: uuid.UUID
    rule_id: uuid.UUID
    device_id: uuid.UUID
    site_id: uuid.UUID
    rule_name: str
    severity: str
    opened_at: float
    peak: float | None


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


class Store:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def load_catalog(self) -> Catalog:
        rules = [
            Rule(
                id=r["id"],
                device_id=r["device_id"],
                name=r["name"],
                kind=r["kind"],
                metric=r["metric"],
                operator=r["operator"],
                threshold=r["threshold"],
                clear_threshold=r["clear_threshold"],
                duration_s=r["duration_s"],
                window_s=r["window_s"],
                severity=r["severity"],
            )
            for r in await self.pool.fetch(RULES_SQL)
        ]
        devices = {r["id"]: r["site_id"] for r in await self.pool.fetch(DEVICES_SQL)}
        return Catalog(rules, devices)

    async def open_alarms(self) -> list[OpenAlarm]:
        return [
            OpenAlarm(
                id=r["id"],
                rule_id=r["rule_id"],
                device_id=r["device_id"],
                site_id=r["site_id"],
                rule_name=r["rule_name"],
                severity=r["severity"],
                opened_at=r["opened_at"].timestamp(),
                peak=r["peak_value"],
            )
            for r in await self.pool.fetch(OPEN_ALARMS_SQL)
        ]

    async def watermarks(self) -> dict[uuid.UUID, float]:
        """Per rule, the time of its latest alarm event (opened, or cleared if it has been)."""
        rows = await self.pool.fetch(WATERMARKS_SQL)
        return {r["rule_id"]: r["last_event"].timestamp() for r in rows}

    async def open_alarm(self, rule: Rule, ts: float, peak: float) -> uuid.UUID | None:
        """The new alarm's id, or None if an alarm for this rule is already open."""
        alarm_id: uuid.UUID | None = await self.pool.fetchval(
            OPEN_SQL, uuid.uuid4(), rule.id, rule.device_id, _dt(ts), peak
        )
        return alarm_id

    async def clear_alarm(self, rule_id: uuid.UUID, ts: float, peak: float) -> uuid.UUID | None:
        """The id of the alarm that was closed, or None if none was open."""
        alarm_id: uuid.UUID | None = await self.pool.fetchval(CLEAR_SQL, rule_id, _dt(ts), peak)
        return alarm_id

    async def samples(self, device_ids: list[uuid.UUID], since: float) -> list[Sample]:
        """The stored readings since `since`, one Sample per (device, time), in time order."""
        out: list[Sample] = []
        current: tuple[Any, Any] | None = None
        metrics: dict[str, float] = {}
        for row in await self.pool.fetch(SAMPLES_SQL, device_ids, _dt(since)):
            key = (row["time"], row["device_id"])
            if key != current:
                if current is not None:
                    out.append(Sample(current[1], current[0].timestamp(), metrics))
                current, metrics = key, {}
            metrics[row["metric"]] = row["value"]
        if current is not None:
            out.append(Sample(current[1], current[0].timestamp(), metrics))
        return out
