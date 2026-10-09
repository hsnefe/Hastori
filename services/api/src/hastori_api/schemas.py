"""Response and request models shared by the routers."""

import math
import uuid
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MIN_PASSWORD = 12
Role = Literal["system_admin", "site_admin", "viewer"]
Severity = Literal["warning", "critical"]
AlarmState = Literal["active", "acknowledged", "cleared"]
Metric = Literal["active_power_kw", "reactive_power_kvar", "current_a", "temperature_c"]
Interval = Literal["auto", "raw", "1m", "1h"]
RuleKind = Literal["threshold", "reactive_ratio", "no_data"]

# A no_data rule: how long a metric may stay absent. Devices publish every 2 s, so less than 10 s
# would alarm on a single lost message.
NO_DATA_MIN_S = 10

MAX_PAGE_SIZE = 100
# Not EmailStr: its validator refuses the reserved `.local` names the demo users have.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    size: int


# -- auth ---------------------------------------------------------------------------------------


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    city: str | None = None
    timezone: str = Field(description="IANA time zone; the day boundary of daily consumption")


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str = Field(description="system_admin, site_admin or viewer")
    org_id: uuid.UUID
    sites: list[SiteOut]


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="seconds until the access token expires")


class WsTicketOut(BaseModel):
    ticket: str = Field(description="single use; open /api/v1/ws?ticket=<this>")
    expires_in: int = Field(description="seconds until the ticket expires")


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class DemoConfigOut(BaseModel):
    enabled: bool = Field(description="whether POST /auth/demo signs anybody in")


class DemoLoginIn(BaseModel):
    role: Literal["viewer", "site_admin"] = Field(description="which demo account to enter as")


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, examples=["izmir.admin@demo.hastori.local"])
    password: str = Field(min_length=1, max_length=256)


# -- sites and devices --------------------------------------------------------------------------


def _valid_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown time zone: {name}") from None
    return name


class SiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Bursa Depo"])
    city: str | None = Field(default=None, max_length=120)
    timezone: str = Field(default="Europe/Istanbul", examples=["Europe/Istanbul"])

    _zone = field_validator("timezone")(_valid_zone)


class SitePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _check_zone(cls, value: str | None) -> str | None:
        return None if value is None else _valid_zone(value)


class LatestValue(BaseModel):
    value: float
    time: datetime


class DeviceOut(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    key: str
    name: str
    type: str
    is_active: bool
    online: bool = Field(description="a reading arrived in the last 30 seconds")
    last_seen: datetime | None = None
    latest: dict[str, LatestValue] = Field(
        description="newest value per metric of the last 10 minutes"
    )


# -- measurements -------------------------------------------------------------------------------


class PointOut(BaseModel):
    time: datetime
    value: float = Field(description="the reading; the average for 1m and 1h")
    min: float | None = None
    max: float | None = None


class SeriesOut(BaseModel):
    device_id: uuid.UUID
    metric: Metric
    interval: Literal["raw", "1m", "1h"]
    start: datetime
    end: datetime
    points: list[PointOut]


class DayConsumption(BaseModel):
    date: date
    kwh: float = Field(description="active energy of the site's main panel(s)")
    coverage: float = Field(
        ge=0, le=1, description="share of the day's minutes that have data (1.0 = complete)"
    )
    reactive_kvarh: float = Field(description="inductive reactive energy")
    reactive_ratio: float | None = Field(
        default=None, description="reactive / active energy of the day; null without active energy"
    )


class ConsumptionOut(BaseModel):
    site_id: uuid.UUID
    timezone: str
    days: list[DayConsumption]


# -- alarm rules --------------------------------------------------------------------------------


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


class RuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Kompresör-1 yüksek sıcaklık"])
    kind: RuleKind = "threshold"
    metric: Metric
    operator: Literal[">", "<"] = Field(description="no_data rules take '>' (left out: '>')")
    threshold: float = Field(description="no_data rules have none (left out: 0)")
    clear_threshold: float = Field(
        description="the alarm closes only past this value (hysteresis); on the right side of "
        "the threshold: at or below it for '>', at or above it for '<'. no_data rules have none "
        "(left out: 0): they close after the metric has been arriving again for 10 seconds"
    )
    duration_s: int = Field(
        ge=0,
        le=600,
        description="how long the breach must last; for no_data how long the metric may stay "
        f"absent ({NO_DATA_MIN_S} to 600)",
    )
    window_s: int | None = Field(
        default=None, description="reactive_ratio only: sliding window, 60 to 3600 seconds"
    )
    severity: Severity
    enabled: bool = True

    _finite_numbers = field_validator("threshold", "clear_threshold")(_finite)

    @model_validator(mode="before")
    @classmethod
    def _no_data_has_no_threshold(cls, data: object) -> object:
        """A silence rule is not about a value: the fields that describe one may be left out."""
        if isinstance(data, dict) and data.get("kind") == "no_data":
            return {"operator": ">", "threshold": 0, "clear_threshold": 0, **data}
        return data

    @model_validator(mode="after")
    def _consistent(self) -> "RuleBody":
        if self.kind == "no_data":
            if self.operator != ">" or self.threshold != 0 or self.clear_threshold != 0:
                raise ValueError("a no_data rule has no threshold: leave the threshold fields out")
            if self.duration_s < NO_DATA_MIN_S:
                raise ValueError(f"a no_data rule needs duration_s of at least {NO_DATA_MIN_S}")
            if self.window_s is not None:
                raise ValueError("window_s applies to reactive_ratio rules only")
            return self
        if self.operator == ">" and self.clear_threshold > self.threshold:
            raise ValueError("clear_threshold must not be above threshold for operator '>'")
        if self.operator == "<" and self.clear_threshold < self.threshold:
            raise ValueError("clear_threshold must not be below threshold for operator '<'")
        if self.kind == "reactive_ratio":
            if self.metric != "reactive_power_kvar" or self.operator != ">":
                raise ValueError("a reactive_ratio rule watches reactive_power_kvar with '>'")
            if self.window_s is None or not 60 <= self.window_s <= 3600:
                raise ValueError("a reactive_ratio rule needs window_s between 60 and 3600")
        elif self.window_s is not None:
            raise ValueError("window_s applies to reactive_ratio rules only")
        return self


class RuleCreate(RuleBody):
    device_id: uuid.UUID


class RuleOut(RuleBody):
    id: uuid.UUID
    device_id: uuid.UUID
    device_name: str
    site_id: uuid.UUID


# -- alarms -------------------------------------------------------------------------------------


class AlarmOut(BaseModel):
    id: uuid.UUID
    state: AlarmState
    severity: Severity
    rule_id: uuid.UUID
    rule_name: str
    metric: str
    device_id: uuid.UUID
    device_name: str
    site_id: uuid.UUID
    site_name: str
    opened_at: datetime
    acked_at: datetime | None = None
    acked_by: uuid.UUID | None = None
    acked_by_label: str | None = Field(
        default=None, description="who acknowledged: the name, never the e-mail address"
    )
    cleared_at: datetime | None = None
    peak_value: float | None = None


class TimelineEntry(BaseModel):
    event: Literal["opened", "acknowledged", "cleared"]
    at: datetime
    by: str | None = Field(default=None, description="who acknowledged (their name)")


class AlarmDetailOut(AlarmOut):
    threshold: float | None = Field(
        default=None, description="the rule's threshold when the alarm opened (null before 0006)"
    )
    clear_threshold: float | None = None
    timeline: list[TimelineEntry]
    rule: RuleOut


# -- users --------------------------------------------------------------------------------------


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    site_ids: list[uuid.UUID]
    is_active: bool = True
    created_at: datetime


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)
    role: Role
    site_ids: list[uuid.UUID] = Field(
        default_factory=list, description="sites the user may see (ignored for a system admin)"
    )


class UserPatch(BaseModel):
    role: Role | None = None
    site_ids: list[uuid.UUID] | None = None
    password: str | None = Field(default=None, min_length=MIN_PASSWORD, max_length=256)
    is_active: bool | None = Field(
        default=None, description="false deactivates: sign-in and every session end at once"
    )
