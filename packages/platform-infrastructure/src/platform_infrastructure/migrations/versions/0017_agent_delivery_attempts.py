"""Bound autonomous repair branches and retain every outcome.

Revision ID: 0017_agent_delivery_attempts
Revises: 0016_agent_science_links
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_agent_delivery_attempts"
down_revision = "0016_agent_science_links"
branch_labels = None
depends_on = None
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "agent_delivery_attempts",
        sa.Column("attempt_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column(
            "tool_request_id",
            ID,
            sa.ForeignKey("agent_tool_requests.tool_request_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("tool_id", sa.String(128), nullable=False),
        sa.Column("branch_key", sa.String(71), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("request_digest", sa.String(71), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("failure_category", sa.String(64)),
        sa.Column("retry_of_attempt_id", ID, sa.ForeignKey("agent_delivery_attempts.attempt_id")),
        sa.Column("stop_reason", sa.String(64)),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("finished_at", TIME),
        sa.UniqueConstraint(
            "agent_run_id", "branch_key", "ordinal", name="uq_agent_branch_ordinal"
        ),
        sa.CheckConstraint(
            "outcome IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN','STOPPED')",
            name="ck_agent_delivery_outcome",
        ),
    )
    op.create_index(
        "ix_agent_delivery_branch",
        "agent_delivery_attempts",
        ["agent_run_id", "branch_key"],
    )


def downgrade():
    op.drop_index("ix_agent_delivery_branch", table_name="agent_delivery_attempts")
    op.drop_table("agent_delivery_attempts")
