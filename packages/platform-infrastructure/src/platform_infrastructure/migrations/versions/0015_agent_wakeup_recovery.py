"""Persist Agent wait dependencies and runtime recovery evidence.

Revision ID: 0015_agent_wakeup_recovery
Revises: 0014_agent_action_links
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_agent_wakeup_recovery"
down_revision = "0014_agent_action_links"
branch_labels = None
depends_on = None
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "agent_wait_dependencies",
        sa.Column("dependency_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("proposal_id", ID, sa.ForeignKey("agent_proposals.proposal_id"), nullable=False),
        sa.Column("dependency_index", sa.Integer, nullable=False),
        sa.Column("owner", sa.String(24), nullable=False),
        sa.Column("object_id", ID, nullable=False),
        sa.Column("minimum_revision", sa.BigInteger),
        sa.Column("observed_revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("state_digest", sa.String(71), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("updated_at", TIME, nullable=False),
        sa.UniqueConstraint("proposal_id", "dependency_index", name="uq_agent_wait_position"),
        sa.CheckConstraint(
            "owner IN ('PLATFORM','DATA','KNOWLEDGE','EXECUTION','ACCEPTANCE','MODEL')",
            name="ck_agent_wait_owner",
        ),
        sa.CheckConstraint(
            "state IN ('WAITING','SATISFIED','UNKNOWN','CANCELLED')",
            name="ck_agent_wait_state",
        ),
    )
    op.create_index("ix_agent_wait_active", "agent_wait_dependencies", ["agent_run_id", "state"])
    op.create_table(
        "agent_source_watermarks",
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
        sa.Column("source", sa.String(32), primary_key=True),
        sa.Column("stream_id", ID, primary_key=True),
        sa.Column("latest_revision", sa.BigInteger, nullable=False),
        sa.Column("latest_event_digest", sa.String(71), nullable=False),
        sa.Column("updated_at", TIME, nullable=False),
    )
    op.create_table(
        "agent_runtime_recoveries",
        sa.Column("recovery_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("from_epoch", sa.Integer, nullable=False),
        sa.Column("to_epoch", sa.Integer, nullable=False),
        sa.Column("reason", sa.String(24), nullable=False),
        sa.Column("ledger_revision", sa.BigInteger, nullable=False),
        sa.Column("ledger_digest", sa.String(71), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.UniqueConstraint("agent_run_id", "to_epoch", name="uq_agent_recovery_epoch"),
    )
    op.create_table(
        "agent_reconciliation_timers",
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
        sa.Column("next_attempt_at", TIME, nullable=False),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_category", sa.String(64)),
        sa.Column("updated_at", TIME, nullable=False),
    )


def downgrade():
    op.drop_table("agent_reconciliation_timers")
    op.drop_table("agent_runtime_recoveries")
    op.drop_table("agent_source_watermarks")
    op.drop_index("ix_agent_wait_active", table_name="agent_wait_dependencies")
    op.drop_table("agent_wait_dependencies")
