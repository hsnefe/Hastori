"""no_data rule kind: an alarm for a device that stopped reporting

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT ck_rules_kind")
    op.execute(
        "ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_kind "
        "CHECK (kind IN ('threshold','reactive_ratio','no_data'))"
    )
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT ck_rules_window")
    # IS NOT NULL is not redundant (see 0005): a CHECK that evaluates to NULL passes.
    op.execute(
        """
        ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_window CHECK (
            (kind = 'reactive_ratio' AND window_s IS NOT NULL AND window_s BETWEEN 60 AND 3600)
            OR (kind IN ('threshold', 'no_data') AND window_s IS NULL)
        )
        """
    )
    # A silence rule has no threshold: duration_s is how long a metric may stay absent. The
    # threshold columns are NOT NULL and tied together by ck_rules_clear, so they hold 0 / '>'.
    # At least 10 s: devices publish every 2 s and a shorter limit would flap on a single loss.
    op.execute(
        """
        ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_no_data CHECK (
            kind <> 'no_data' OR (
                operator = '>' AND threshold = 0 AND clear_threshold = 0
                AND duration_s BETWEEN 10 AND 600
            )
        )
        """
    )


def downgrade() -> None:
    # Without the kind a silence rule would read as "metric > 0": switch them off first. Rules
    # are never deleted (alarms refer to them).
    # The old constraints come back NOT VALID: the switched-off no_data rows stay and would
    # otherwise fail them.
    op.execute("UPDATE alarm_rules SET enabled = false WHERE kind = 'no_data'")
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT ck_rules_no_data")
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT ck_rules_window")
    op.execute(
        """
        ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_window CHECK (
            (kind = 'reactive_ratio' AND window_s IS NOT NULL AND window_s BETWEEN 60 AND 3600)
            OR (kind = 'threshold' AND window_s IS NULL)
        ) NOT VALID
        """
    )
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT ck_rules_kind")
    op.execute(
        "ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_kind "
        "CHECK (kind IN ('threshold','reactive_ratio')) NOT VALID"
    )
