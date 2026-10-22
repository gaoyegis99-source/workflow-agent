"""Initial P2 control schema. The snapshot is independent of future table changes."""

from alembic import op
from platform_infrastructure.migrations.schema_0001 import metadata

revision = "0001_control"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    metadata.drop_all(op.get_bind())
