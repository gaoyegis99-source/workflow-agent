"""Append-only workflow diagnostics owned by Execution."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_workflow_trace"
down_revision = "0002_workflows"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_trace_events",
        sa.Column("event_key", sa.String(64), primary_key=True),
        sa.Column("workflow_execution_id", sa.String(128), nullable=False),
        sa.Column("node_action_id", sa.String(128)),
        sa.Column("engine_epoch", sa.Integer),
        sa.Column("worker_generation", sa.BigInteger),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("source_monotonic_ns", sa.BigInteger),
        sa.Column("duration_ms", sa.Float),
        sa.Column("attributes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["executions.execution_id"], name="workflow_trace_parent"
        ),
        sa.ForeignKeyConstraint(
            ["node_action_id"], ["executions.action_id"], name="workflow_trace_node"
        ),
    )
    op.create_index(
        "workflow_trace_parent_time",
        "workflow_trace_events",
        ["workflow_execution_id", "observed_at"],
    )
    op.create_index(
        "workflow_trace_node_time",
        "workflow_trace_events",
        ["node_action_id", "observed_at"],
    )


def downgrade():
    op.drop_index("workflow_trace_node_time", table_name="workflow_trace_events")
    op.drop_index("workflow_trace_parent_time", table_name="workflow_trace_events")
    op.drop_table("workflow_trace_events")
