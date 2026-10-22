"""Deterministic agent coordination ledger.

Revision ID: 0011_agent_coordination
Revises: 0010_checker_runs
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_agent_coordination"
down_revision = "0010_checker_runs"
branch_labels = None
depends_on = None
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "agent_runs",
        sa.Column("agent_run_id", ID, primary_key=True),
        sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("generation", sa.BigInteger, nullable=False),
        sa.Column("runtime_ref", ID, nullable=False, unique=True),
        sa.Column("runtime_profile", JSONB, nullable=False),
        sa.Column("runtime_profile_digest", sa.String(71), nullable=False),
        sa.Column("task_spec_revision", sa.Integer, nullable=False),
        sa.Column("control_version", sa.Integer, nullable=False),
        sa.Column("plan_revision", sa.Integer, nullable=False, server_default="0"),
        sa.Column("frame_sequence", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("accepted_proposals", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("wake_revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("blocked_reason", sa.String(64)),
        sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("updated_at", TIME, nullable=False),
        sa.CheckConstraint(
            "status IN ('READY','ADVANCING','WAITING','QUESTION','BLOCKED','STOPPED')",
            name="ck_agent_run_status",
        ),
        sa.CheckConstraint(
            "epoch > 0 AND generation > 0 AND task_spec_revision > 0 "
            "AND control_version > 0 AND plan_revision >= 0 AND frame_sequence >= 0 "
            "AND accepted_proposals >= 0 AND wake_revision >= 0",
            name="ck_agent_run_revisions",
        ),
    )
    op.create_table(
        "agent_plan_revisions",
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("task_spec_revision", sa.Integer, nullable=False),
        sa.Column("frame_id", ID, nullable=False),
        sa.Column("generation", sa.BigInteger, nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("plan_digest", sa.String(71), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_agent_plan_revision"),
    )
    op.create_table(
        "agent_context_manifests",
        sa.Column("manifest_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("task_spec_revision", sa.Integer, nullable=False),
        sa.Column("manifest", JSONB, nullable=False),
        sa.Column("manifest_digest", sa.String(71), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.UniqueConstraint(
            "agent_run_id", "manifest_digest", name="uq_agent_context_manifest_digest"
        ),
    )
    op.create_table(
        "agent_decision_frames",
        sa.Column("frame_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("sequence", sa.BigInteger, nullable=False),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("generation", sa.BigInteger, nullable=False),
        sa.Column("task_spec_revision", sa.Integer, nullable=False),
        sa.Column("control_version", sa.Integer, nullable=False),
        sa.Column(
            "context_manifest_id",
            ID,
            sa.ForeignKey("agent_context_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column("context_manifest_digest", sa.String(71), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("frame_digest", sa.String(71), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.UniqueConstraint("agent_run_id", "sequence", name="uq_agent_frame_sequence"),
        sa.CheckConstraint("state IN ('PENDING','ACCEPTED','STALE')", name="ck_agent_frame_state"),
    )
    op.create_table(
        "agent_proposals",
        sa.Column("proposal_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column(
            "frame_id",
            ID,
            sa.ForeignKey("agent_decision_frames.frame_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("generation", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("proposal", JSONB, nullable=False),
        sa.Column("proposal_digest", sa.String(71), nullable=False),
        sa.Column("accepted_at", TIME, nullable=False),
    )
    op.create_table(
        "agent_questions",
        sa.Column("question_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("frame_id", ID, sa.ForeignKey("agent_decision_frames.frame_id"), nullable=False),
        sa.Column("gap_id", ID, nullable=False),
        sa.Column("question", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.UniqueConstraint("agent_run_id", "gap_id", name="uq_agent_question_gap"),
        sa.CheckConstraint(
            "status IN ('OPEN','ANSWERED','CANCELLED')", name="ck_agent_question_status"
        ),
    )
    op.create_table(
        "agent_answers",
        sa.Column("answer_id", ID, primary_key=True),
        sa.Column(
            "question_id",
            ID,
            sa.ForeignKey("agent_questions.question_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("task_spec_revision", sa.Integer, nullable=False),
        sa.Column("answer", JSONB, nullable=False),
        sa.Column("answer_digest", sa.String(71), nullable=False),
        sa.Column("answered_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
        sa.Column("answered_at", TIME, nullable=False),
    )
    op.create_table(
        "agent_events",
        sa.Column("event_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("stream_id", ID, nullable=False),
        sa.Column("source_revision", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("event_digest", sa.String(71), nullable=False),
        sa.Column("observed_at", TIME, nullable=False),
        sa.UniqueConstraint(
            "source", "stream_id", "source_revision", name="uq_agent_event_revision"
        ),
    )
    op.create_table(
        "agent_inbox",
        sa.Column("source", sa.String(32), primary_key=True),
        sa.Column("event_id", ID, primary_key=True),
        sa.Column("event_digest", sa.String(71), nullable=False),
        sa.Column("observed_at", TIME, nullable=False),
    )
    op.create_table(
        "agent_wakeups",
        sa.Column("wake_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("revision", sa.BigInteger, nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("event_ids", JSONB, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("consumed_at", TIME),
        sa.UniqueConstraint("agent_run_id", "revision", name="uq_agent_wakeup_revision"),
        sa.CheckConstraint(
            "state IN ('PENDING','CONSUMED','CANCELLED')", name="ck_agent_wakeup_state"
        ),
    )
    op.create_table(
        "agent_outbox",
        sa.Column("event_id", ID, primary_key=True),
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("done", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("available_at", TIME, nullable=False),
        sa.Column("owner", ID),
        sa.Column("lease_until", TIME),
        sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.create_table(
        "agent_leases",
        sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
        sa.Column("owner", ID, nullable=False),
        sa.Column("generation", sa.BigInteger, nullable=False),
        sa.Column("lease_until", TIME, nullable=False),
        sa.Column("acquired_at", TIME, nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_agent_lease_generation"),
    )
    op.create_index("ix_agent_runs_ready", "agent_runs", ["status", "updated_at"])
    op.create_index("ix_agent_frames_run", "agent_decision_frames", ["agent_run_id", "sequence"])
    op.create_index("ix_agent_events_run", "agent_events", ["agent_run_id", "observed_at"])
    op.create_index(
        "ix_agent_wakeups_pending",
        "agent_wakeups",
        ["agent_run_id", "revision"],
        postgresql_where=sa.text("state = 'PENDING'"),
    )
    op.create_index(
        "ix_agent_outbox_ready",
        "agent_outbox",
        ["available_at"],
        postgresql_where=sa.text("done = false"),
    )


def downgrade():
    op.drop_index("ix_agent_outbox_ready", table_name="agent_outbox")
    op.drop_index("ix_agent_wakeups_pending", table_name="agent_wakeups")
    op.drop_index("ix_agent_events_run", table_name="agent_events")
    op.drop_index("ix_agent_frames_run", table_name="agent_decision_frames")
    op.drop_index("ix_agent_runs_ready", table_name="agent_runs")
    for name in [
        "agent_leases",
        "agent_outbox",
        "agent_wakeups",
        "agent_inbox",
        "agent_events",
        "agent_answers",
        "agent_questions",
        "agent_proposals",
        "agent_decision_frames",
        "agent_context_manifests",
        "agent_plan_revisions",
        "agent_runs",
    ]:
        op.drop_table(name)
