from alembic import op

from platform_execution.migrations.schema_0001 import metadata

revision = "0001_execution"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    metadata.drop_all(op.get_bind())
