"""alarms keep the thresholds they opened with

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The rule can be edited after the alarm opened; the alarm must still say what it tripped.
    # Alarms from before this migration stay NULL: their rule may have changed, a guess would lie.
    op.execute("ALTER TABLE alarms ADD COLUMN threshold double precision")
    op.execute("ALTER TABLE alarms ADD COLUMN clear_threshold double precision")


def downgrade() -> None:
    op.execute("ALTER TABLE alarms DROP COLUMN IF EXISTS clear_threshold")
    op.execute("ALTER TABLE alarms DROP COLUMN IF EXISTS threshold")
