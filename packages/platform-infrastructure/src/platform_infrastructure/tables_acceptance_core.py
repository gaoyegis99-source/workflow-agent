"""Core Deliverable and Acceptance aggregate table declarations."""

from .tables_shared import ID, JSONB, TIME, sa, table

deliverables = table(
    "deliverables",
    sa.Column("deliverable_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("identity_mode", sa.String(16), nullable=False, server_default="EXACT"),
    sa.CheckConstraint("identity_mode = 'EXACT'", name="ck_deliverable_identity_mode"),
    sa.UniqueConstraint("task_id", "spec_revision", "digest"),
)
acceptances = table(
    "acceptances",
    sa.Column("acceptance_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("deliverable_id", ID, sa.ForeignKey("deliverables.deliverable_id"), nullable=False),
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), nullable=True, unique=True),
    sa.Column(
        "analysis_registration_id", ID, sa.ForeignKey("analysis_registrations.registration_id")
    ),
    sa.Column("retry_of", ID),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("report", JSONB),
    sa.Column("report_digest", sa.String(64)),
    sa.Column("owner", ID),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", TIME),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
)
sa.Index(
    "ix_acceptances_worker_ready",
    acceptances.c.status,
    acceptances.c.lease_until,
    acceptances.c.acceptance_id,
)
completions = table(
    "completions",
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), primary_key=True),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), nullable=False),
    sa.Column("report_digest", sa.String(64), nullable=False),
    sa.Column("completed_at", TIME, nullable=False, server_default=sa.func.now()),
)
