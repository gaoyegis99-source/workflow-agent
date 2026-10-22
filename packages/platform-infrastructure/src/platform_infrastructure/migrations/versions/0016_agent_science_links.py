"""Allow stable Agent links to existing research and workflow use cases.

Revision ID: 0016_agent_science_links
Revises: 0015_agent_wakeup_recovery
"""

from alembic import op

revision = "0016_agent_science_links"
down_revision = "0015_agent_wakeup_recovery"
branch_labels = None
depends_on = None

OPERATIONS = (
    "'SUBMIT_OPERATOR','SUBMIT_WORKFLOW','REGISTER_DELIVERABLE','REQUEST_ACCEPTANCE',"
    "'CREATE_ANALYSIS','REGISTER_ANALYSIS','CREATE_CLAIM'"
)


def upgrade():
    op.drop_constraint("ck_agent_action_link_operation", "agent_action_links", type_="check")
    op.create_check_constraint(
        "ck_agent_action_link_operation",
        "agent_action_links",
        f"operation IN ({OPERATIONS})",
    )


def downgrade():
    op.drop_constraint("ck_agent_action_link_operation", "agent_action_links", type_="check")
    op.create_check_constraint(
        "ck_agent_action_link_operation",
        "agent_action_links",
        "operation IN ('SUBMIT_OPERATOR','REGISTER_DELIVERABLE','REQUEST_ACCEPTANCE')",
    )
