"""Execution-owned invocation ancestry; no Platform table access."""

import sqlalchemy as sa
from alembic import op

revision = "0002_workflows"
down_revision = "0001_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("executions", sa.Column("parent_execution_id", sa.String(128)))
    op.add_column("executions", sa.Column("invocation_key", sa.String(128)))
    op.create_foreign_key(
        "execution_parent", "executions", "executions", ["parent_execution_id"], ["execution_id"]
    )
    op.create_unique_constraint(
        "execution_invocation", "executions", ["parent_execution_id", "invocation_key"]
    )


def downgrade():
    op.drop_constraint("execution_invocation", "executions")
    op.drop_constraint("execution_parent", "executions")
    op.drop_column("executions", "invocation_key")
    op.drop_column("executions", "parent_execution_id")
