"""Single source of truth: seed/demo.yaml, plus device credential derivation."""

import hashlib
import hmac
from pathlib import Path
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, field_validator

DEFAULT_SEED_FILE = Path(__file__).resolve().parents[4] / "seed" / "demo.yaml"

METRICS = ("active_power_kw", "reactive_power_kvar", "current_a", "temperature_c")


class OrgSeed(BaseModel):
    id: UUID
    name: str


class SiteSeed(BaseModel):
    id: UUID
    key: str
    name: str
    city: str | None = None
    timezone: str = "Europe/Istanbul"

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
            raise ValueError(f"unknown time zone {value!r}") from None
        return value


class DeviceSeed(BaseModel):
    id: UUID
    key: str
    site: str
    name: str
    type: str


class UserSeed(BaseModel):
    id: UUID
    email: str
    role: str
    password_env: str
    sites: list[str]


class RuleSeed(BaseModel):
    id: UUID
    device: str
    name: str
    metric: str
    operator: str
    threshold: float
    duration_s: int
    clear_threshold: float
    severity: str
    kind: Literal["threshold", "reactive_ratio", "no_data"] = "threshold"
    window_s: int | None = None


class SeedData(BaseModel):
    organization: OrgSeed
    sites: list[SiteSeed]
    devices: list[DeviceSeed]
    users: list[UserSeed]
    alarm_rules: list[RuleSeed]

    def site_by_key(self, key: str) -> SiteSeed:
        return next(s for s in self.sites if s.key == key)

    def device_by_key(self, key: str) -> DeviceSeed:
        return next(d for d in self.devices if d.key == key)

    def topic(self, device: DeviceSeed) -> str:
        return device_topic(self.site_by_key(device.site).id, device.id)


def load_seed(path: Path | None = None) -> SeedData:
    raw = yaml.safe_load((path or DEFAULT_SEED_FILE).read_text(encoding="utf-8"))
    return SeedData.model_validate(raw)


def device_topic(site_id: UUID, device_id: UUID) -> str:
    return f"sites/{site_id}/devices/{device_id}/telemetry"


def derive_device_password(secret: str, device_id: UUID) -> str:
    """Device MQTT password: HMAC-SHA256(secret, device_id), hex. Never stored."""
    return hmac.new(secret.encode(), str(device_id).encode(), hashlib.sha256).hexdigest()
