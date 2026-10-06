"""core schema

Revision ID: 0001
Revises:
"""

from alembic import op

from hastori_common.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = [
    "organizations",
    "sites",
    "users",
    "user_sites",
    "devices",
    "alarm_rules",
    "alarms",
    "audit_log",
]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind, tables=[Base.metadata.tables[t] for t in TABLES])


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind, tables=[Base.metadata.tables[t] for t in reversed(TABLES)])
