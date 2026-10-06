"""Response and request models shared by the routers."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system_admin", "site_admin", "viewer"]
Severity = Literal["warning", "critical"]
AlarmState = Literal["active", "acknowledged", "cleared"]
Metric = Literal["active_power_kw", "reactive_power_kvar", "current_a", "temperature_c"]


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    size: int


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


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, examples=["izmir.admin@demo.hastori.local"])
    password: str = Field(min_length=1, max_length=256)
