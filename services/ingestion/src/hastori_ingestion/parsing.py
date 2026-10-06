"""Topic parsing and payload validation. Pure functions, no I/O."""

import json
import math
import uuid
from dataclasses import dataclass
from typing import Any

# A message may legitimately be old: after an outage the broker replays what it queued for the
# persistent session. The session lives SESSION_EXPIRY_S (service.py) = 1 h, so anything the broker
# still holds is at most that old. The window must cover it, or a restart longer than the
# window would throw the backlog away. It stays below the 2 h refresh window of the aggregate.
MAX_AGE_S = 3900.0
MAX_FUTURE_S = 30.0
MAX_PAYLOAD_BYTES = 4096

BOUNDS: dict[str, tuple[float, float]] = {
    "active_power_kw": (0.0, 1000.0),
    # Negative = capacitive (over-compensated): a real operating state, not an error.
    "reactive_power_kvar": (-1000.0, 1000.0),
    "current_a": (0.0, 2000.0),
    "temperature_c": (-40.0, 150.0),
}


class Rejected(Exception):
    """A message that must not be stored. `reason` is a short, bounded label for metrics."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Telemetry:
    site_id: uuid.UUID
    device_id: uuid.UUID
    ts: float
    metrics: dict[str, float]
    message_id: uuid.UUID


def parse_topic(topic: str) -> tuple[uuid.UUID, uuid.UUID]:
    """sites/{site_id}/devices/{device_id}/telemetry -> (site_id, device_id)."""
    parts = topic.split("/")
    if len(parts) != 5 or parts[0] != "sites" or parts[2] != "devices" or parts[4] != "telemetry":
        raise Rejected("bad_topic")
    try:
        return uuid.UUID(parts[1]), uuid.UUID(parts[3])
    except ValueError:
        raise Rejected("bad_topic") from None


def message_id(device_id: uuid.UUID, ts: float) -> uuid.UUID:
    """Deterministic id: the same device reading always maps to the same id (ms resolution)."""
    return uuid.uuid5(device_id, f"{ts:.3f}")


def _finite_number(value: Any) -> bool:
    """True for a real, finite int/float. Huge ints overflow `math.isfinite`: not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def parse_payload(raw: bytes | str, now: float) -> tuple[float, dict[str, float]]:
    """Validate a payload; returns (ts, metrics) or raises Rejected.

    Whatever a device sends, this raises Rejected and nothing else: an unexpected exception here
    would drop the MQTT connection and, because the message is never acked, be redelivered
    forever.
    """
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise Rejected("too_large", str(len(raw)))
    try:
        data: Any = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError, OverflowError):
        raise Rejected("invalid_json") from None
    # Unknown extra fields are ignored: new device firmware must not break ingestion.
    if not isinstance(data, dict) or "ts" not in data or "metrics" not in data:
        raise Rejected("invalid_payload", "expected ts and metrics")

    ts = data["ts"]
    if not _finite_number(ts):
        raise Rejected("invalid_payload", "ts must be a finite number")
    if ts < now - MAX_AGE_S:
        raise Rejected("stale_ts")
    if ts > now + MAX_FUTURE_S:
        raise Rejected("future_ts")

    metrics = data["metrics"]
    if not isinstance(metrics, dict) or not metrics:
        raise Rejected("invalid_payload", "metrics must be a non-empty object")
    clean: dict[str, float] = {}
    for name, value in metrics.items():
        if name not in BOUNDS:
            raise Rejected("unknown_metric", str(name)[:40])
        if not _finite_number(value):
            raise Rejected("bad_value", name)
        lo, hi = BOUNDS[name]
        if not lo <= value <= hi:
            raise Rejected("out_of_range", name)
        clean[name] = float(value)
    return round(float(ts), 3), clean


def build_telemetry(
    site_id: uuid.UUID, device_id: uuid.UUID, ts: float, metrics: dict[str, float]
) -> Telemetry:
    return Telemetry(site_id, device_id, ts, metrics, message_id(device_id, ts))
