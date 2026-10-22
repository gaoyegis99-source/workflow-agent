"""Publication group membership, committed with all visible assets."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_publication_groups"
down_revision = "0001_data"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publication_members",
        sa.Column("execution_id", sa.String(128), primary_key=True),
        sa.Column("member_id", sa.String(128), primary_key=True),
        sa.Column("asset_ref", JSONB, nullable=False),
    )


def downgrade():
    op.drop_table("publication_members")
