"""Separate workflow supervision, node execution and publication work."""

import sqlalchemy as sa
from alembic import op

revision = "0004_execution_work_class"
down_revision = "0003_workflow_trace"
branch_labels = None
depends_on = None

CLASSES = ("WORKFLOW_SUPERVISOR", "NODE_EXECUTION", "PUBLICATION_RECONCILE")
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED")


def upgrade():
    op.add_column(
        "executions",
        sa.Column(
            "work_class",
            sa.String(32),
            nullable=False,
            server_default="NODE_EXECUTION",
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE executions
            SET work_class = CASE
              WHEN runtime ->> 'phase' = 'PUBLISHING' THEN 'PUBLICATION_RECONCILE'
              WHEN payload ->> 'kind' = 'WORKFLOW'
                   AND parent_execution_id IS NULL THEN 'WORKFLOW_SUPERVISOR'
              ELSE 'NODE_EXECUTION'
            END
            """
        )
    )
    op.create_check_constraint(
        "execution_work_class_valid",
        "executions",
        "work_class IN ('WORKFLOW_SUPERVISOR','NODE_EXECUTION','PUBLICATION_RECONCILE')",
    )
    op.create_index(
        "execution_work_ready",
        "executions",
        ["work_class", "available_at"],
        postgresql_where=sa.text(
            "state NOT IN ('SUCCEEDED','FAILED','CANCELLED','REJECTED','SEALED')"
        ),
    )


def downgrade():
    op.drop_index("execution_work_ready", table_name="executions")
    op.drop_constraint("execution_work_class_valid", "executions", type_="check")
    op.drop_column("executions", "work_class")
