"""alarm rule sanity checks, case-insensitive unique email, list-query indexes

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A rule that can never clear (clear_threshold on the wrong side of threshold) or that
    # references a metric the system does not store is a bug waiting to page someone.
    op.execute(
        "ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_duration CHECK (duration_s >= 0)"
    )
    op.execute(
        "ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_metric CHECK (metric IN "
        "('active_power_kw','reactive_power_kvar','current_a','temperature_c'))"
    )
    op.execute(
        """
        ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_clear CHECK (
            clear_threshold IS NULL
            OR (operator = '>' AND clear_threshold <= threshold)
            OR (operator = '<' AND clear_threshold >= threshold)
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))")
    op.execute("CREATE INDEX ix_alarm_rules_device ON alarm_rules (device_id)")
    op.execute("CREATE INDEX ix_alarms_device_opened ON alarms (device_id, opened_at DESC)")
    op.execute("CREATE INDEX ix_alarms_state ON alarms (state) WHERE state <> 'cleared'")
    op.execute("CREATE INDEX ix_audit_log_created ON audit_log (created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_log_created")
    op.execute("DROP INDEX IF EXISTS ix_alarms_state")
    op.execute("DROP INDEX IF EXISTS ix_alarms_device_opened")
    op.execute("DROP INDEX IF EXISTS ix_alarm_rules_device")
    op.execute("DROP INDEX IF EXISTS uq_users_email_lower")
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT IF EXISTS ck_rules_clear")
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT IF EXISTS ck_rules_metric")
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT IF EXISTS ck_rules_duration")
