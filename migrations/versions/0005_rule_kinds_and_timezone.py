"""rule kinds (threshold, reactive_ratio), mandatory clear threshold, rule change notifications,
site time zone

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A derived rule (reactive energy / active energy over a sliding window) is a different kind
    # of rule from a threshold on one measured value.
    op.execute(
        "ALTER TABLE alarm_rules ADD COLUMN kind text NOT NULL DEFAULT 'threshold' "
        "CONSTRAINT ck_rules_kind CHECK (kind IN ('threshold','reactive_ratio'))"
    )
    op.execute("ALTER TABLE alarm_rules ADD COLUMN window_s integer")
    # IS NOT NULL is not redundant: a CHECK that evaluates to NULL passes, and NULL BETWEEN a AND b
    # is NULL.
    op.execute(
        """
        ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_window CHECK (
            (kind = 'reactive_ratio' AND window_s IS NOT NULL AND window_s BETWEEN 60 AND 3600)
            OR (kind = 'threshold' AND window_s IS NULL)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_reactive CHECK (
            kind <> 'reactive_ratio' OR (metric = 'reactive_power_kvar' AND operator = '>')
        )
        """
    )

    # Hysteresis is mandatory: without a clear threshold a value hovering at the threshold opens
    # and closes the alarm over and over. Existing rules get a 5 % band.
    op.execute(
        """
        UPDATE alarm_rules
        SET clear_threshold = CASE operator
            WHEN '>' THEN threshold - abs(threshold) * 0.05
            ELSE threshold + abs(threshold) * 0.05
        END
        WHERE clear_threshold IS NULL
        """
    )
    op.execute("ALTER TABLE alarm_rules ALTER COLUMN clear_threshold SET NOT NULL")

    # Two enabled rules on the same thing would raise a warning and a critical alarm for one
    # event; a second severity is a second threshold on the same rule, not a second alarm.
    op.execute(
        "CREATE UNIQUE INDEX uq_rules_device_metric_kind ON alarm_rules "
        "(device_id, metric, kind) WHERE enabled"
    )

    # The alarm service keeps the rules in memory and reloads on this notification.
    op.execute(
        """
        CREATE FUNCTION notify_alarm_rules_changed() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify(
                'alarm_rules_changed',
                (CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END)::text
            );
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER alarm_rules_changed AFTER INSERT OR UPDATE OR DELETE ON alarm_rules
        FOR EACH ROW EXECUTE FUNCTION notify_alarm_rules_changed()
        """
    )

    # The day boundary of daily consumption is the site's, not the server's.
    op.execute("ALTER TABLE sites ADD COLUMN timezone text NOT NULL DEFAULT 'Europe/Istanbul'")


def downgrade() -> None:
    op.execute("ALTER TABLE sites DROP COLUMN IF EXISTS timezone")
    op.execute("DROP TRIGGER IF EXISTS alarm_rules_changed ON alarm_rules")
    op.execute("DROP FUNCTION IF EXISTS notify_alarm_rules_changed()")
    op.execute("DROP INDEX IF EXISTS uq_rules_device_metric_kind")
    op.execute("ALTER TABLE alarm_rules ALTER COLUMN clear_threshold DROP NOT NULL")
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT IF EXISTS ck_rules_reactive")
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT IF EXISTS ck_rules_window")
    op.execute("ALTER TABLE alarm_rules DROP COLUMN IF EXISTS window_s")
    op.execute("ALTER TABLE alarm_rules DROP COLUMN IF EXISTS kind")
