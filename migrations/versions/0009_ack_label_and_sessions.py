"""acknowledger's name on the alarm; token version and active flag on users

- alarms.acked_by_label: the acknowledging user's name (the e-mail address before the @) as it
  was when they acknowledged. The API shows this instead of the e-mail address, and it stays
  when the account is renamed or removed (risk G14).
- users.token_version: raised by a password change or a deactivation; tokens issued under an
  older version stop working (risk G3).
- users.is_active: a deactivated user cannot sign in and holds no session.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alarms ADD COLUMN IF NOT EXISTS acked_by_label text")
    op.execute(
        "UPDATE alarms a SET acked_by_label = split_part(u.email, '@', 1) "
        "FROM users u WHERE a.acked_by = u.id AND a.acked_by_label IS NULL"
    )
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_active")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS token_version")
    op.execute("ALTER TABLE alarms DROP COLUMN IF EXISTS acked_by_label")
