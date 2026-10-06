"""SQLAlchemy 2 models. measurements and its aggregate are created in migration 0002."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

METRICS = ("active_power_kw", "reactive_power_kvar", "current_a", "temperature_c")


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(Text)
    # IANA name; the day boundary of the daily consumption.
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'Europe/Istanbul'")
    )
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("org_id", "name"),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        CheckConstraint("role IN ('system_admin','site_admin','viewer')", name="ck_users_role"),
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )


class UserSite(Base):
    __tablename__ = "user_sites"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sites.id"), primary_key=True)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[uuid.UUID] = _uuid_pk()
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sites.id"), nullable=False)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    mqtt_topic: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("site_id", "name"),)


class AlarmRule(Base):
    __tablename__ = "alarm_rules"
    id: Mapped[uuid.UUID] = _uuid_pk()
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    # Mandatory: the alarm clears only below it, which keeps a value that hovers at the threshold
    # from opening and closing the alarm over and over.
    clear_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    # threshold: one measured value against a limit. reactive_ratio: reactive / active energy over
    # a sliding window of window_s seconds (energy analyzers only).
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'threshold'"))
    window_s: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    __table_args__ = (
        CheckConstraint("operator IN ('>','<')", name="ck_rules_operator"),
        CheckConstraint("severity IN ('warning','critical')", name="ck_rules_severity"),
        CheckConstraint("duration_s >= 0", name="ck_rules_duration"),
        CheckConstraint(
            "metric IN ('active_power_kw','reactive_power_kvar','current_a','temperature_c')",
            name="ck_rules_metric",
        ),
        CheckConstraint(
            "clear_threshold IS NULL OR (operator = '>' AND clear_threshold <= threshold)"
            " OR (operator = '<' AND clear_threshold >= threshold)",
            name="ck_rules_clear",
        ),
        CheckConstraint("kind IN ('threshold','reactive_ratio')", name="ck_rules_kind"),
        CheckConstraint(
            "(kind = 'reactive_ratio' AND window_s IS NOT NULL AND window_s BETWEEN 60 AND 3600)"
            " OR (kind = 'threshold' AND window_s IS NULL)",
            name="ck_rules_window",
        ),
        CheckConstraint(
            "kind <> 'reactive_ratio' OR (metric = 'reactive_power_kvar' AND operator = '>')",
            name="ck_rules_reactive",
        ),
        Index("ix_alarm_rules_device", "device_id"),
        # One enabled rule per device, metric and kind: no warning + critical pair for one event.
        Index(
            "uq_rules_device_metric_kind",
            "device_id",
            "metric",
            "kind",
            unique=True,
            postgresql_where=text("enabled"),
        ),
    )


class Alarm(Base):
    __tablename__ = "alarms"
    id: Mapped[uuid.UUID] = _uuid_pk()
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alarm_rules.id"), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    peak_value: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        CheckConstraint("state IN ('active','acknowledged','cleared')", name="ck_alarms_state"),
        Index(
            "uq_alarms_open_rule",
            "rule_id",
            unique=True,
            postgresql_where=text("state IN ('active','acknowledged')"),
        ),
        Index("ix_alarms_device_opened", "device_id", text("opened_at DESC")),
        Index("ix_alarms_state", "state", postgresql_where=text("state <> 'cleared'")),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = _created()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (Index("ix_audit_log_created", text("created_at DESC")),)


class Outbox(Base):
    """Events committed together with measurements; a relay publishes them to RabbitMQ."""

    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False)
    routing_key: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()
