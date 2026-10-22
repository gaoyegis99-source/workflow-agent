"""Persist stable P6.3 read requests and observations.

Revision ID: 0013_agent_read_tools
Revises: 0012_agent_model_calls
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013_agent_read_tools"
down_revision = "0012_agent_model_calls"
branch_labels = None
depends_on = None
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "agent_tool_requests",
        sa.Column("tool_request_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("proposal_id", ID, sa.ForeignKey("agent_proposals.proposal_id"), nullable=False),
        sa.Column("frame_id", ID, sa.ForeignKey("agent_decision_frames.frame_id"), nullable=False),
        sa.Column("proposal_index", sa.Integer, nullable=False),
        sa.Column("model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id")),
        sa.Column("call_id", ID, nullable=False),
        sa.Column("tool_id", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(1000), nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("request_digest", sa.String(71), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("updated_at", TIME, nullable=False),
        sa.UniqueConstraint("proposal_id", "proposal_index", name="uq_agent_tool_proposal_index"),
        sa.CheckConstraint("proposal_index >= 0", name="ck_agent_tool_proposal_index"),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED')",
            name="ck_agent_tool_request_status",
        ),
    )
    op.create_table(
        "agent_tool_observations",
        sa.Column("observation_id", ID, primary_key=True),
        sa.Column(
            "tool_request_id",
            ID,
            sa.ForeignKey("agent_tool_requests.tool_request_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("result_digest", sa.String(71), nullable=False),
        sa.Column("refs", JSONB, nullable=False),
        sa.Column("error_category", sa.String(64)),
        sa.Column("contains_untrusted_data", sa.Boolean, nullable=False),
        sa.Column("observed_at", TIME, nullable=False),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED','DENIED','NOT_FOUND','TEMPORARILY_UNAVAILABLE','FAILED')",
            name="ck_agent_tool_observation_status",
        ),
    )
    op.create_index(
        "ix_agent_tool_requests_run", "agent_tool_requests", ["agent_run_id", "created_at"]
    )


def downgrade():
    op.drop_index("ix_agent_tool_requests_run", table_name="agent_tool_requests")
    op.drop_table("agent_tool_observations")
    op.drop_table("agent_tool_requests")
