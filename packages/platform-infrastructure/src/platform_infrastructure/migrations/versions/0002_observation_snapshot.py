"""Add a nullable observation snapshot without reconstructing historical facts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_observation_snapshot"
down_revision = "0001_control"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("actions", sa.Column("last_external_observation", JSONB, nullable=True))


def downgrade():
    op.drop_column("actions", "last_external_observation")
