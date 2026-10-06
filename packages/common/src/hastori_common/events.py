"""Alarm events published to Redis pub/sub and read by the WebSocket gateway (day 3).

Pub/sub gives no delivery guarantee: a client that reconnects fetches the current state over REST
and treats events as hints, never as the source of truth.
"""

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field

EventType = Literal["alarm.opened", "alarm.acknowledged", "alarm.cleared"]


def channel(site_id: uuid.UUID | str) -> str:
    """One channel per site: a subscriber receives only the sites it is allowed to see."""
    return f"hastori:site:{site_id}"


class AlarmEvent(BaseModel):
    type: EventType
    alarm_id: uuid.UUID
    rule_id: uuid.UUID
    rule_name: str
    device_id: uuid.UUID
    site_id: uuid.UUID
    severity: Literal["warning", "critical"]
    state: Literal["active", "acknowledged", "cleared"]
    # opened: value that opened it; acknowledged: none; cleared: the peak during the alarm
    value: float | None = None
    ts: float = Field(default_factory=time.time, description="event time, epoch seconds")
