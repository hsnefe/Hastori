"""outbox for RabbitMQ events, device change notifications, device delete guard

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE outbox (
            id bigserial PRIMARY KEY,
            message_id uuid NOT NULL UNIQUE,
            routing_key text NOT NULL,
            body text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Ingestion keeps a device cache; it listens on this channel to refresh immediately.
    op.execute(
        """
        CREATE FUNCTION notify_devices_changed() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('devices_changed', '');
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER devices_changed AFTER INSERT OR UPDATE OR DELETE ON devices
        FOR EACH STATEMENT EXECUTE FUNCTION notify_devices_changed()
        """
    )
    # Devices own history (measurements, alarms): deactivate instead of deleting.
    op.execute(
        """
        CREATE FUNCTION forbid_device_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'devices are never deleted; set is_active = false';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER devices_no_delete BEFORE DELETE ON devices
        FOR EACH ROW EXECUTE FUNCTION forbid_device_delete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS devices_no_delete ON devices")
    op.execute("DROP FUNCTION IF EXISTS forbid_device_delete()")
    op.execute("DROP TRIGGER IF EXISTS devices_changed ON devices")
    op.execute("DROP FUNCTION IF EXISTS notify_devices_changed()")
    op.execute("DROP TABLE IF EXISTS outbox")
