"""core schema

Frozen DDL: this migration must not import the ORM models. From day 2 on, schema changes go in
new migrations only; editing this file would make clean installs diverge from upgraded ones.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _id() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True)


def _created() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table("organizations", _id(), sa.Column("name", sa.Text, nullable=False), _created())
    op.create_table(
        "sites",
        _id(),
        sa.Column("org_id", pg.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("city", sa.Text),
        _created(),
        sa.UniqueConstraint("org_id", "name"),
    )
    op.create_table(
        "users",
        _id(),
        sa.Column("org_id", pg.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        _created(),
        sa.CheckConstraint("role IN ('system_admin','site_admin','viewer')", name="ck_users_role"),
    )
    op.create_table(
        "user_sites",
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("site_id", pg.UUID(as_uuid=True), sa.ForeignKey("sites.id"), primary_key=True),
    )
    op.create_table(
        "devices",
        _id(),
        sa.Column("site_id", pg.UUID(as_uuid=True), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("key", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("mqtt_topic", sa.Text, nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        _created(),
        sa.UniqueConstraint("site_id", "name"),
    )
    op.create_table(
        "alarm_rules",
        _id(),
        sa.Column("device_id", pg.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("metric", sa.Text, nullable=False),
        sa.Column("operator", sa.Text, nullable=False),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("duration_s", sa.Integer, nullable=False),
        sa.Column("clear_threshold", sa.Float),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("operator IN ('>','<')", name="ck_rules_operator"),
        sa.CheckConstraint("severity IN ('warning','critical')", name="ck_rules_severity"),
    )
    op.create_table(
        "alarms",
        _id(),
        sa.Column("rule_id", pg.UUID(as_uuid=True), sa.ForeignKey("alarm_rules.id"), nullable=False),
        sa.Column("device_id", pg.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True)),
        sa.Column("acked_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column("peak_value", sa.Float),
        sa.CheckConstraint("state IN ('active','acknowledged','cleared')", name="ck_alarms_state"),
    )
    op.create_index(
        "uq_alarms_open_rule",
        "alarms",
        ["rule_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('active','acknowledged')"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _created(),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("entity", sa.Text),
        sa.Column("entity_id", sa.Text),
        sa.Column("detail", pg.JSONB),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_index("uq_alarms_open_rule", table_name="alarms")
    for table in ("alarms", "alarm_rules", "devices", "user_sites", "users", "sites", "organizations"):
        op.drop_table(table)
