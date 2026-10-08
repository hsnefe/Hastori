"""cap the rule duration in the database too

The API accepts at most 600 s, but a seed or a SQL statement could set any number. The start-up
replay reads `duration_s + 60` seconds of raw samples into memory (256 MiB container).

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT VALID first: the constraint applies to new rows at once and the check of the existing
    # ones is a separate step, so a bad old row names itself instead of failing the whole ALTER.
    op.execute(
        "ALTER TABLE alarm_rules ADD CONSTRAINT ck_rules_duration_max "
        "CHECK (duration_s <= 600) NOT VALID"
    )
    op.execute("ALTER TABLE alarm_rules VALIDATE CONSTRAINT ck_rules_duration_max")


def downgrade() -> None:
    op.execute("ALTER TABLE alarm_rules DROP CONSTRAINT IF EXISTS ck_rules_duration_max")
