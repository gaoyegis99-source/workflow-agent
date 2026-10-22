"""Agent model budget, calls and provider reconciliation.

Revision ID: 0012_agent_model_calls
Revises: 0011_agent_coordination
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_agent_model_calls"
down_revision = "0011_agent_coordination"
branch_labels = None
depends_on = None
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "agent_model_budgets",
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False, unique=True),
        sa.Column("profile_id", ID, nullable=False),
        sa.Column("profile_digest", sa.String(71), nullable=False),
        sa.Column("max_model_calls", sa.Integer, nullable=False),
        sa.Column("max_input_tokens", sa.BigInteger, nullable=False),
        sa.Column("max_output_tokens", sa.BigInteger, nullable=False),
        sa.Column("max_reasoning_tokens", sa.BigInteger),
        sa.Column("max_estimated_cost_microunits", sa.BigInteger, nullable=False),
        sa.Column("max_agent_wall_seconds", sa.Integer, nullable=False),
        sa.Column("max_decision_frames", sa.Integer, nullable=False),
        sa.Column("used_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reserved_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("used_input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reserved_input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("used_output_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reserved_output_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("used_reasoning_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reserved_reasoning_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("used_cost_microunits", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reserved_cost_microunits", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", TIME, nullable=False),
        sa.CheckConstraint(
            "max_model_calls > 0 AND max_input_tokens > 0 AND max_output_tokens > 0 "
            "AND (max_reasoning_tokens IS NULL OR max_reasoning_tokens >= 0) "
            "AND max_estimated_cost_microunits > 0 AND max_agent_wall_seconds > 0 "
            "AND max_decision_frames > 0",
            name="ck_agent_model_budget_limits",
        ),
        sa.CheckConstraint(
            "used_calls >= 0 AND reserved_calls >= 0 AND used_input_tokens >= 0 "
            "AND reserved_input_tokens >= 0 AND used_output_tokens >= 0 "
            "AND reserved_output_tokens >= 0 AND used_reasoning_tokens >= 0 "
            "AND reserved_reasoning_tokens >= 0 AND used_cost_microunits >= 0 "
            "AND reserved_cost_microunits >= 0",
            name="ck_agent_model_budget_usage",
        ),
    )
    op.create_table(
        "agent_model_calls",
        sa.Column("model_call_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("frame_id", ID, sa.ForeignKey("agent_decision_frames.frame_id"), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("repair_of_model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id")),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("generation", sa.BigInteger, nullable=False),
        sa.Column("profile_id", ID, nullable=False),
        sa.Column("profile_digest", sa.String(71), nullable=False),
        sa.Column("context_digest", sa.String(71), nullable=False),
        sa.Column("tool_catalog_digest", sa.String(71), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("request_digest", sa.String(71), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("provider_ref", ID),
        sa.Column("provider_revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("response", JSONB),
        sa.Column("response_digest", sa.String(71)),
        sa.Column("proposal", JSONB),
        sa.Column("proposal_digest", sa.String(71)),
        sa.Column("parse_errors", JSONB),
        sa.Column("usage_status", sa.String(24), nullable=False),
        sa.Column("contains_sensitive_context", sa.Boolean, nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("sending_at", TIME),
        sa.Column("accepted_at", TIME),
        sa.Column("first_byte_at", TIME),
        sa.Column("completed_at", TIME),
        sa.UniqueConstraint("frame_id", "attempt", name="uq_agent_model_call_attempt"),
        sa.CheckConstraint("attempt IN (1,2)", name="ck_agent_model_call_attempt"),
        sa.CheckConstraint(
            "status IN ('RESERVED','SUBMITTING','ACCEPTED','COMPLETED','RETRYABLE',"
            "'UNKNOWN','REJECTED','ERROR','CANCELLED')",
            name="ck_agent_model_call_status",
        ),
        sa.CheckConstraint(
            "usage_status IN ('RESERVED','KNOWN','UNKNOWN','NOT_EXECUTED')",
            name="ck_agent_model_usage_status",
        ),
    )
    op.create_table(
        "agent_model_reservations",
        sa.Column(
            "model_call_id",
            ID,
            sa.ForeignKey("agent_model_calls.model_call_id"),
            primary_key=True,
        ),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("reserved_input_tokens", sa.BigInteger, nullable=False),
        sa.Column("reserved_output_tokens", sa.BigInteger, nullable=False),
        sa.Column("reserved_reasoning_tokens", sa.BigInteger, nullable=False),
        sa.Column("reserved_cost_microunits", sa.BigInteger, nullable=False),
        sa.Column("actual_input_tokens", sa.BigInteger),
        sa.Column("actual_output_tokens", sa.BigInteger),
        sa.Column("actual_reasoning_tokens", sa.BigInteger),
        sa.Column("actual_cost_microunits", sa.BigInteger),
        sa.Column("call_charged", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("settled_at", TIME),
        sa.CheckConstraint(
            "reserved_input_tokens >= 0 AND reserved_output_tokens >= 0 "
            "AND reserved_reasoning_tokens >= 0 AND reserved_cost_microunits >= 0",
            name="ck_agent_model_reservation_values",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','UNKNOWN_USAGE','SETTLED','RELEASED')",
            name="ck_agent_model_reservation_status",
        ),
    )
    op.create_table(
        "agent_model_observations",
        sa.Column("source_event_id", ID, primary_key=True),
        sa.Column(
            "model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id"), nullable=False
        ),
        sa.Column("provider_revision", sa.BigInteger, nullable=False),
        sa.Column("provider_ref", ID),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("executed", sa.Boolean),
        sa.Column("response_digest", sa.String(71)),
        sa.Column("usage", JSONB),
        sa.Column("observation_digest", sa.String(71), nullable=False),
        sa.Column("observed_at", TIME, nullable=False),
        sa.UniqueConstraint(
            "model_call_id", "provider_revision", name="uq_agent_model_observation_revision"
        ),
    )
    op.create_table(
        "model_provider_reconciliation",
        sa.Column(
            "model_call_id",
            ID,
            sa.ForeignKey("agent_model_calls.model_call_id"),
            primary_key=True,
        ),
        sa.Column("next_attempt_at", TIME, nullable=False),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("owner", ID),
        sa.Column("lease_until", TIME),
        sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
        sa.CheckConstraint("failure_count >= 0 AND generation >= 0", name="ck_model_reconcile"),
    )
    op.create_index("ix_agent_model_calls_run", "agent_model_calls", ["agent_run_id", "created_at"])
    op.create_index(
        "ix_model_reconciliation_ready",
        "model_provider_reconciliation",
        ["next_attempt_at"],
    )


def downgrade():
    op.drop_index("ix_model_reconciliation_ready", table_name="model_provider_reconciliation")
    op.drop_index("ix_agent_model_calls_run", table_name="agent_model_calls")
    op.drop_table("model_provider_reconciliation")
    op.drop_table("agent_model_observations")
    op.drop_table("agent_model_reservations")
    op.drop_table("agent_model_calls")
    op.drop_table("agent_model_budgets")
