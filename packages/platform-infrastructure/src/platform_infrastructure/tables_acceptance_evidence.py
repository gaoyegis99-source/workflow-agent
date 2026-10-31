"""Acceptance coverage, checker, review, and evidence declarations.

The tables retain the shared registry from ``tables_shared`` and are re-exported
through ``tables`` for repository compatibility.  This module intentionally has
no migration logic: Alembic history remains the authority for schema changes.
"""

from .tables_shared import ID, JSONB, TIME, metadata, sa, table

coverage_plans = table(
    "acceptance_coverage_plans",
    sa.Column("plan_id", ID, primary_key=True),
    sa.Column(
        "acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), nullable=False, unique=True
    ),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("delivery_class", sa.String(16), nullable=False),
    sa.Column("source_digest", sa.String(64), nullable=False),
    sa.Column("plan", JSONB, nullable=False),
    sa.Column("plan_digest", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
)
coverage_items = table(
    "acceptance_coverage_items",
    sa.Column("plan_id", ID, sa.ForeignKey("acceptance_coverage_plans.plan_id"), primary_key=True),
    sa.Column("item_id", ID, primary_key=True),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("requirement_id", ID, nullable=False),
    sa.Column("check_type", sa.String(32), nullable=False),
    sa.Column("necessary", sa.Boolean, nullable=False),
    sa.Column("automatic", sa.Boolean, nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("result", JSONB),
    sa.Column("coverage_status", sa.String(16), nullable=False, server_default="COVERED"),
    sa.Column("capability_id", ID),
    sa.Column("capability_version", sa.String(32)),
    sa.Column("capability_digest", sa.String(71)),
    sa.Column("check_run_id", ID),
    sa.UniqueConstraint("plan_id", "ordinal", name="uq_coverage_ordinal"),
)

check_envelopes = table(
    "acceptance_check_envelopes",
    sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("amount", sa.Integer, nullable=False),
    sa.Column("released", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("token_id", ID, sa.ForeignKey("tokens.token_id")),
    sa.Column("plan_digest", sa.String(64), nullable=False),
    sa.Column("catalog_digest", sa.String(71), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint("amount > 0", name="ck_acceptance_check_envelope_amount"),
)
check_runs = table(
    "acceptance_check_runs",
    sa.Column("run_id", ID, primary_key=True),
    sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), nullable=False),
    sa.Column("run_key", ID, nullable=False),
    sa.Column("capability_id", ID, nullable=False),
    sa.Column("capability_version", sa.String(32), nullable=False),
    sa.Column("capability_digest", sa.String(71), nullable=False),
    sa.Column("implementation_id", ID, nullable=False),
    sa.Column("image_digest", sa.String(71), nullable=False),
    sa.Column("input_contract_digest", sa.String(71), nullable=False),
    sa.Column("output_contract_digest", sa.String(71), nullable=False),
    sa.Column("plan_digest", sa.String(64), nullable=False),
    sa.Column("item_ids", JSONB, nullable=False),
    sa.Column("input_roles", JSONB, nullable=False),
    sa.Column("claim_revision_ids", JSONB, nullable=False),
    sa.Column("depends_on", JSONB, nullable=False),
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), unique=True),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("retry_of_run_id", ID, sa.ForeignKey("acceptance_check_runs.run_id")),
    sa.Column("report", JSONB),
    sa.Column("report_digest", sa.String(64)),
    sa.UniqueConstraint("acceptance_id", "run_key", name="uq_acceptance_check_run_key"),
)
human_reviews = table(
    "acceptance_human_reviews",
    sa.Column("review_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), nullable=False),
    sa.Column("plan_id", ID, sa.ForeignKey("acceptance_coverage_plans.plan_id"), nullable=False),
    sa.Column("plan_revision", sa.Integer, nullable=False),
    sa.Column("item_id", ID, nullable=False),
    sa.Column("reviewer_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("qualification", sa.String(500), nullable=False),
    sa.Column("criteria", sa.String(2000), nullable=False),
    sa.Column("decision", sa.String(16), nullable=False),
    sa.Column("reason", sa.String(2000), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint("plan_id", "item_id", name="uq_human_review_item"),
)
acceptance_evidence = table(
    "acceptance_evidence_refs",
    sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), primary_key=True),
    sa.Column("evidence_digest", sa.String(64), primary_key=True),
    sa.Column("project_id", ID, nullable=False),
    sa.Column("document_id", ID, nullable=False),
    sa.Column("version_id", ID, nullable=False),
    sa.Column("evidence_ref", JSONB, nullable=False),
)
completion_validity = table(
    "completion_validity",
    sa.Column("task_id", ID, sa.ForeignKey("completions.task_id"), primary_key=True),
    sa.Column("validity", sa.String(16), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("reasons", JSONB, nullable=False),
    sa.Column("updated_at", TIME, nullable=False),
)
knowledge_lifecycle_cursors = table(
    "knowledge_lifecycle_cursors",
    sa.Column("project_id", ID, primary_key=True),
    sa.Column("document_id", ID, primary_key=True),
    sa.Column("version_id", ID, primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("event_id", ID, nullable=False),
)
sa.Index("ix_coverage_task", coverage_plans.c.task_id, coverage_plans.c.acceptance_id)
sa.Index(
    "ix_acceptance_evidence_version",
    acceptance_evidence.c.project_id,
    acceptance_evidence.c.document_id,
    acceptance_evidence.c.version_id,
)

permits = sa.Table(
    "execution_permits",
    metadata,
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), primary_key=True),
    sa.Column("execution_id", ID, nullable=False),
    sa.Column("attempt_id", ID, nullable=False),
    sa.Column(
        "issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
)
