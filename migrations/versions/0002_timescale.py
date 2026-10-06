"""measurements hypertable, 1m continuous aggregate, retention

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# Every step is idempotent: the continuous aggregate must be created outside a transaction, so a
# failure after the table exists cannot be rolled back and a rerun has to be able to resume.
#
# WARNING for operators: never run refresh_continuous_aggregate('measurements_1m', NULL, NULL).
# Raw data is kept 7 days; refreshing a wider window than that rebuilds the aggregate from the
# raw data that is left and deletes the older, already materialised minutes.
def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS measurements (
            time timestamptz NOT NULL,
            device_id uuid NOT NULL REFERENCES devices(id),
            metric text NOT NULL
                CHECK (metric IN ('active_power_kw','reactive_power_kvar','current_a','temperature_c')),
            value double precision NOT NULL
        )
        """
    )
    op.execute(
        "SELECT create_hypertable('measurements', 'time', "
        "chunk_time_interval => INTERVAL '1 day', if_not_exists => true)"
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_measurements ON measurements (device_id, metric, time DESC)")

    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS measurements_1m
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT time_bucket('1 minute', time) AS bucket,
                   device_id, metric,
                   avg(value) AS avg_value, min(value) AS min_value, max(value) AS max_value
            FROM measurements
            GROUP BY bucket, device_id, metric
            WITH NO DATA
            """
        )
        op.execute(
            """
            SELECT add_continuous_aggregate_policy('measurements_1m',
                if_not_exists => true,
                start_offset => INTERVAL '2 hours',
                end_offset => INTERVAL '1 minute',
                schedule_interval => INTERVAL '1 minute')
            """
        )
    op.execute("SELECT add_retention_policy('measurements', INTERVAL '7 days', if_not_exists => true)")
    op.execute("SELECT add_retention_policy('measurements_1m', INTERVAL '90 days', if_not_exists => true)")


def downgrade() -> None:
    op.execute("SELECT remove_retention_policy('measurements_1m', if_exists => true)")
    op.execute("SELECT remove_retention_policy('measurements', if_exists => true)")
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS measurements_1m")
    op.execute("DROP TABLE IF EXISTS measurements")
