"""Persist recoverable spatial import runtime facts.

Revision ID: 0003_spatial_import_runtime
Revises: 0002_publication_groups
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_spatial_import_runtime"
down_revision = "0002_publication_groups"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "uploads",
        sa.Column("runtime", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade():
    op.drop_column("uploads", "runtime")
