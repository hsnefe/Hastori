"""Alarm events the API publishes (the alarm service publishes opened and cleared)."""

import asyncio
import logging

from redis.asyncio import Redis

from hastori_api.schemas import AlarmOut
from hastori_common.events import AlarmEvent, channel

log = logging.getLogger("api.events")


async def publish_acknowledged(redis: Redis, alarm: AlarmOut) -> None:
    """After the commit, best effort: a screen that misses the event shows the right state the
    next time it reads the alarm over REST."""
    event = AlarmEvent(
        type="alarm.acknowledged",
        alarm_id=alarm.id,
        rule_id=alarm.rule_id,
        rule_name=alarm.rule_name,
        device_id=alarm.device_id,
        site_id=alarm.site_id,
        severity=alarm.severity,
        state="acknowledged",
        ts=alarm.acked_at.timestamp() if alarm.acked_at else 0.0,
    )
    try:
        await asyncio.wait_for(redis.publish(channel(alarm.site_id), event.model_dump_json()), 2)
    except Exception as exc:
        log.warning("alarm event not published", extra={"error": str(exc)})
