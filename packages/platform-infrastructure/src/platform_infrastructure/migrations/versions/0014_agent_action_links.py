"""Allow token-free Agent actions and persist business identity links.

Revision ID: 0014_agent_action_links
Revises: 0013_agent_read_tools
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_agent_action_links"
down_revision = "0013_agent_read_tools"
branch_labels = None
depends_on = None
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def upgrade():
    op.alter_column("actions", "token_id", existing_type=ID, nullable=True)
    op.drop_constraint("actions_decision_source_check", "actions", type_="check")
    op.create_check_constraint(
        "ck_actions_decision_source", "actions", "decision_source IN ('CLIENT','AGENT')"
    )
    op.create_table(
        "agent_action_links",
        sa.Column("link_id", ID, primary_key=True),
        sa.Column(
            "tool_request_id",
            ID,
            sa.ForeignKey("agent_tool_requests.tool_request_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("proposal_id", ID, sa.ForeignKey("agent_proposals.proposal_id"), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("business_id", ID, nullable=False),
        sa.Column("action_id", ID, sa.ForeignKey("actions.action_id")),
        sa.Column("command_id", ID, sa.ForeignKey("commands.command_id")),
        sa.Column("request_digest", sa.String(71), nullable=False),
        sa.Column("observation_revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_observation_digest", sa.String(71)),
        sa.Column("created_at", TIME, nullable=False),
        sa.CheckConstraint(
            "operation IN ('SUBMIT_OPERATOR','REGISTER_DELIVERABLE','REQUEST_ACCEPTANCE')",
            name="ck_agent_action_link_operation",
        ),
    )
    op.create_index(
        "ix_agent_action_links_run", "agent_action_links", ["agent_run_id", "created_at"]
    )


def downgrade():
    op.drop_index("ix_agent_action_links_run", table_name="agent_action_links")
    op.drop_table("agent_action_links")
    op.drop_constraint("ck_actions_decision_source", "actions", type_="check")
    op.create_check_constraint(
        "actions_decision_source_check", "actions", "decision_source = 'CLIENT'"
    )
    op.alter_column("actions", "token_id", existing_type=ID, nullable=False)
