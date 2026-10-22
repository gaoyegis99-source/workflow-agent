"""Platform-owned relational schema; no external execution tables."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def table(name, *columns, **kwargs):
    return sa.Table(name, metadata, *columns, **kwargs)


principals = table(
    "principals",
    sa.Column("principal_id", ID, primary_key=True),
    sa.Column("name", sa.String(200), nullable=False),
)
tokens = table(
    "tokens",
    sa.Column("token_id", ID, primary_key=True),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("digest", sa.String(64), unique=True, nullable=False),
    sa.Column("expires_at", TIME, nullable=False),
    sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
)
projects = table(
    "projects",
    sa.Column("project_id", ID, primary_key=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("created_at", TIME, nullable=False, server_default=sa.func.now()),
)
members = table(
    "members",
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), primary_key=True),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), primary_key=True),
    sa.Column("role", sa.String(16), nullable=False),
    sa.CheckConstraint("role IN ('OWNER','EDITOR','VIEWER')"),
)
tasks = table(
    "tasks",
    sa.Column("task_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("control_version", sa.Integer, nullable=False),
    sa.Column("lifecycle", sa.String(16), nullable=False),
    sa.Column("control", sa.String(16), nullable=False),
    sa.Column("cleanup_status", sa.String(16), nullable=False),
    sa.Column("grant_epoch", sa.Integer, nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint("spec_revision > 0 AND control_version > 0 AND grant_epoch > 0"),
    sa.CheckConstraint("lifecycle IN ('OPEN','CANCELLED','COMPLETED')"),
    sa.CheckConstraint("control IN ('ACTIVE','PAUSED')"),
    sa.CheckConstraint("cleanup_status IN ('CLEAR','PENDING','UNKNOWN')"),
)
specs = table(
    "task_specs",
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("spec", JSONB, nullable=False),
    sa.Column("actor_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
)
grants = table(
    "grants",
    sa.Column("grant_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("targets", JSONB, nullable=False),
    sa.Column("resources", JSONB, nullable=False),
    sa.Column("expires_at", TIME, nullable=False),
    sa.Column("revoked", sa.Boolean, nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("derived_outputs", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("acceptance_profiles", JSONB, nullable=False, server_default="[]"),
    sa.Column("revoked_at", TIME),
    sa.Column("revoked_by", ID, sa.ForeignKey("principals.principal_id")),
)
budgets = table(
    "budgets",
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), primary_key=True),
    *[
        sa.Column(name, sa.BigInteger, nullable=False)
        for name in ["execution_count", "max_in_flight", "used", "reserved", "in_flight"]
    ],
    sa.CheckConstraint("execution_count >= 0 AND max_in_flight > 0"),
    sa.CheckConstraint("used >= 0 AND reserved >= 0 AND in_flight >= 0"),
    sa.CheckConstraint("used + reserved <= execution_count AND in_flight <= max_in_flight"),
)
commands = table(
    "commands",
    sa.Column("command_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("action_id", ID),
    sa.Column("execution_id", ID),
    sa.Column("error_code", sa.String(64)),
    sa.Column("error_message", sa.String(512)),
)
actions = table(
    "actions",
    sa.Column("action_id", ID, primary_key=True),
    sa.Column("command_id", ID, sa.ForeignKey("commands.command_id"), unique=True, nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("grant_epoch", sa.Integer, nullable=False),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("token_id", ID, sa.ForeignKey("tokens.token_id"), nullable=True),
    sa.Column("frame_id", ID, unique=True, nullable=False),
    sa.Column("decision_source", sa.String(16), nullable=False),
    sa.CheckConstraint("decision_source IN ('CLIENT','AGENT')", name="ck_actions_decision_source"),
    sa.Column("request", JSONB, nullable=False),
    sa.Column("send_started", sa.Boolean, nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("external_revision", sa.BigInteger, nullable=False),
    sa.Column("execution_id", ID),
    sa.Column("stopped", sa.Boolean, nullable=False),
    sa.Column("cancel_requested", sa.Boolean, nullable=False),
    sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_external_observation", JSONB, nullable=True),
)
reservations = table(
    "reservations",
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("charged", sa.Boolean, nullable=False),
    sa.Column("stopped", sa.Boolean, nullable=False),
)
settlements = table(
    "budget_settlements",
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), primary_key=True),
    sa.Column("source", sa.String(32), primary_key=True),
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("charged", sa.Boolean, nullable=False),
    sa.Column("stopped", sa.Boolean, nullable=False),
    sa.Column("recorded_at", TIME, nullable=False, server_default=sa.func.now()),
)
requests = table(
    "idempotency",
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), primary_key=True),
    sa.Column("scope", ID, primary_key=True),
    sa.Column("operation", sa.String(100), primary_key=True),
    sa.Column("key", sa.String(128), primary_key=True),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("result", JSONB),
)
outbox = table(
    "outbox",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("done", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("available_at", TIME, nullable=False, server_default=sa.func.now()),
    sa.Column("owner", ID),
    sa.Column("lease_until", TIME),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
)
inbox = table(
    "inbox",
    sa.Column("source", sa.String(32), primary_key=True),
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("digest", sa.String(64), nullable=False),
)
audit = table(
    "task_events",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
)
sa.Index("outbox_ready", outbox.c.available_at, postgresql_where=outbox.c.done.is_(False))
sa.Index("actions_task_active", actions.c.task_id, postgresql_where=actions.c.stopped.is_(False))
sa.Index("tasks_project_page", tasks.c.project_id, tasks.c.task_id)

# Each payload is owned by its named Platform module; services never read these tables.
deliverables = table(
    "deliverables",
    sa.Column("deliverable_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("identity_mode", sa.String(16), nullable=False, server_default="LEGACY"),
    sa.CheckConstraint("identity_mode IN ('LEGACY','EXACT')", name="ck_deliverable_identity_mode"),
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
        "analysis_registration_id",
        ID,
        sa.ForeignKey("analysis_registrations.registration_id"),
    ),
    sa.Column("retry_of", ID),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("report", JSONB),
    sa.Column("report_digest", sa.String(64)),
)
completions = table(
    "completions",
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), primary_key=True),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), nullable=False),
    sa.Column("report_digest", sa.String(64), nullable=False),
    sa.Column("completed_at", TIME, nullable=False, server_default=sa.func.now()),
)

# P5.3 keeps the historical acceptance report immutable. Coverage state and
# current completion validity live in separate append/audit-friendly records.
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

# R2 reserves the maximum number of actual checker starts once, then acquires a
# normal computation slot only when each dependency-qualified run is admitted.
check_envelopes = table(
    "acceptance_check_envelopes",
    sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("amount", sa.Integer, nullable=False),
    sa.Column("released", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    # Controlled Agent writes authenticate the current project principal but
    # intentionally carry no user bearer token.
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

workflow_packages = table(
    "workflow_packages",
    sa.Column("package_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("request", JSONB, nullable=False),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("compiled", JSONB),
    sa.Column("error", JSONB),
    sa.Column("owner", ID),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", TIME),
)

workflow_definitions = table(
    "workflow_definitions",
    sa.Column("definition_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("stable_key", ID, nullable=False),
    sa.Column("head_revision", sa.Integer, nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint("head_revision > 0", name="ck_workflow_definitions_head_positive"),
    sa.UniqueConstraint("project_id", "stable_key", name="uq_workflow_definitions_stable_key"),
)
workflow_revisions = table(
    "workflow_revisions",
    sa.Column(
        "definition_id",
        ID,
        sa.ForeignKey("workflow_definitions.definition_id"),
        primary_key=True,
    ),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("source", JSONB, nullable=False),
    sa.Column("source_digest", sa.String(71), nullable=False),
    sa.Column("metadata", JSONB, nullable=False),
    sa.Column("layout", JSONB, nullable=False),
    sa.Column("layout_digest", sa.String(71), nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint("revision > 0", name="ck_workflow_revisions_revision_positive"),
)
sa.Index(
    "ix_workflow_definitions_page",
    workflow_definitions.c.project_id,
    workflow_definitions.c.definition_id,
)
sa.Index(
    "ix_workflow_revisions_page",
    workflow_revisions.c.definition_id,
    workflow_revisions.c.revision,
)
authoring_validations = table(
    "authoring_validations",
    sa.Column("validation_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("definition_id", ID, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("source_digest", sa.String(71), nullable=False),
    sa.Column("source", JSONB, nullable=False),
    sa.Column("context", JSONB, nullable=False),
    sa.Column("context_digest", sa.String(71), nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("issues", JSONB, nullable=False),
    sa.Column("owner", ID),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", TIME),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("started_at", TIME),
    sa.Column("finished_at", TIME),
    sa.ForeignKeyConstraint(
        ["definition_id", "revision"],
        ["workflow_revisions.definition_id", "workflow_revisions.revision"],
        name="fk_authoring_validation_revision",
    ),
    sa.CheckConstraint(
        "status IN ('QUEUED','RUNNING','PASSED','REJECTED','ERROR')",
        name="ck_authoring_validation_status",
    ),
)
sa.Index(
    "ix_authoring_validations_project",
    authoring_validations.c.project_id,
    authoring_validations.c.validation_id,
)
sa.Index(
    "ix_authoring_validations_queue",
    authoring_validations.c.status,
    authoring_validations.c.lease_until,
    authoring_validations.c.created_at,
)
workflow_candidates = table(
    "workflow_candidates",
    sa.Column("candidate_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("definition_id", ID, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column(
        "validation_id",
        ID,
        sa.ForeignKey("authoring_validations.validation_id"),
        nullable=False,
    ),
    sa.Column(
        "package_id",
        ID,
        sa.ForeignKey("workflow_packages.package_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("source_digest", sa.String(71), nullable=False),
    sa.Column("expected_manifest", JSONB, nullable=False),
    sa.Column("expected_manifest_digest", sa.String(71), nullable=False),
    sa.Column("context", JSONB, nullable=False),
    sa.Column("context_digest", sa.String(71), nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.ForeignKeyConstraint(
        ["definition_id", "revision"],
        ["workflow_revisions.definition_id", "workflow_revisions.revision"],
        name="fk_workflow_candidate_revision",
    ),
)
sa.Index(
    "ix_workflow_candidates_page",
    workflow_candidates.c.project_id,
    workflow_candidates.c.definition_id,
    workflow_candidates.c.candidate_id,
)
workflow_trials = table(
    "workflow_trials",
    sa.Column("trial_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column(
        "definition_id", ID, sa.ForeignKey("workflow_definitions.definition_id"), nullable=False
    ),
    sa.Column(
        "candidate_id", ID, sa.ForeignKey("workflow_candidates.candidate_id"), nullable=False
    ),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), nullable=False, unique=True),
    sa.Column("inputs", JSONB, nullable=False),
    sa.Column("inputs_digest", sa.String(71), nullable=False),
    sa.Column("trial_case", JSONB, nullable=False),
    sa.Column("trial_case_digest", sa.String(71), nullable=False),
    sa.Column("context", JSONB, nullable=False),
    sa.Column("context_digest", sa.String(71), nullable=False),
    sa.Column("case_verdict", sa.String(16), nullable=False),
    sa.Column("case_result", JSONB),
    sa.Column("case_result_digest", sa.String(71)),
    sa.Column("error_category", sa.String(128)),
    sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
    sa.Column("next_retry_at", TIME),
    sa.Column("owner", ID),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", TIME),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("finished_at", TIME),
    sa.CheckConstraint(
        "case_verdict IN ('PENDING','PASS','FAIL','ERROR')",
        name="ck_workflow_trial_verdict",
    ),
)
sa.Index(
    "ix_workflow_trials_page",
    workflow_trials.c.project_id,
    workflow_trials.c.definition_id,
    workflow_trials.c.trial_id,
)
sa.Index(
    "ix_workflow_trials_queue",
    workflow_trials.c.case_verdict,
    workflow_trials.c.next_retry_at,
    workflow_trials.c.lease_until,
    workflow_trials.c.created_at,
)
workflow_releases = table(
    "workflow_releases",
    sa.Column("release_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column(
        "definition_id", ID, sa.ForeignKey("workflow_definitions.definition_id"), nullable=False
    ),
    sa.Column("version_label", sa.String(64), nullable=False),
    sa.Column("version_sort", sa.ARRAY(sa.String(96)), nullable=False),
    sa.Column(
        "candidate_id", ID, sa.ForeignKey("workflow_candidates.candidate_id"), nullable=False
    ),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("source_digest", sa.String(71), nullable=False),
    sa.Column("package_ref", JSONB, nullable=False),
    sa.Column("context", JSONB, nullable=False),
    sa.Column("context_digest", sa.String(71), nullable=False),
    sa.Column("lifecycle", sa.String(16), nullable=False, server_default="ACTIVE"),
    sa.Column("lifecycle_revision", sa.Integer, nullable=False, server_default="1"),
    sa.Column("lifecycle_reason", sa.String(512), nullable=False, server_default="INITIAL_RELEASE"),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint(
        "project_id", "definition_id", "version_label", name="uq_workflow_release_version"
    ),
)
sa.Index(
    "ix_workflow_releases_page",
    workflow_releases.c.project_id,
    workflow_releases.c.definition_id,
    workflow_releases.c.version_sort,
    workflow_releases.c.release_id,
)
workflow_release_trials = table(
    "workflow_release_trials",
    sa.Column("release_id", ID, sa.ForeignKey("workflow_releases.release_id"), primary_key=True),
    sa.Column("trial_id", ID, sa.ForeignKey("workflow_trials.trial_id"), primary_key=True),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("trial_case_digest", sa.String(71), nullable=False),
    sa.Column("case_result_digest", sa.String(71), nullable=False),
    sa.UniqueConstraint("release_id", "ordinal", name="uq_workflow_release_trial_ordinal"),
)
workflow_release_lifecycle_events = table(
    "workflow_release_lifecycle_events",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("release_id", ID, sa.ForeignKey("workflow_releases.release_id"), nullable=False),
    sa.Column("lifecycle_revision", sa.Integer, nullable=False),
    sa.Column("previous_lifecycle", sa.String(16)),
    sa.Column("lifecycle", sa.String(16), nullable=False),
    sa.Column("reason", sa.String(512), nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint(
        "release_id", "lifecycle_revision", name="uq_workflow_release_lifecycle_revision"
    ),
)
sa.Index(
    "ix_workflow_release_lifecycle_history",
    workflow_release_lifecycle_events.c.release_id,
    workflow_release_lifecycle_events.c.lifecycle_revision,
)
workflow_release_publication_permits = table(
    "workflow_release_publication_permits",
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), primary_key=True),
    sa.Column("release_id", ID, sa.ForeignKey("workflow_releases.release_id"), nullable=False),
    sa.Column("lifecycle_revision", sa.Integer, nullable=False),
    sa.Column("decision", sa.String(8), nullable=False),
    sa.Column("reason", sa.String(64), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
)
sa.Index(
    "ix_workflow_release_permits_release",
    workflow_release_publication_permits.c.release_id,
    workflow_release_publication_permits.c.action_id,
)

# Capability Studio A4 records inert registration material and append-only
# review/deployment evidence.  None of these tables are an execution registry;
# current executability is projected against the deployed release lock.
operator_proposals = table(
    "operator_proposals",
    sa.Column("proposal_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("stable_key", ID, nullable=False),
    sa.Column("head_revision", sa.Integer, nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint("head_revision > 0", name="ck_operator_proposals_head_positive"),
    sa.UniqueConstraint("project_id", "stable_key", name="uq_operator_proposals_stable_key"),
)
operator_proposal_revisions = table(
    "operator_proposal_revisions",
    sa.Column("proposal_id", ID, sa.ForeignKey("operator_proposals.proposal_id"), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("materials", JSONB, nullable=False),
    sa.Column("materials_digest", sa.String(71), nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint("revision > 0", name="ck_operator_proposal_revision_positive"),
)
operator_review_events = table(
    "operator_review_events",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("proposal_id", ID, sa.ForeignKey("operator_proposals.proposal_id"), nullable=False),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("materials_digest", sa.String(71), nullable=False),
    sa.Column("event_type", sa.String(32), nullable=False),
    sa.Column("reason", sa.String(4000), nullable=False),
    sa.Column("actor_kind", sa.String(16), nullable=False),
    sa.Column("actor_id", ID, nullable=False),
    sa.Column("evidence_digest", sa.String(71)),
    sa.Column("created_at", TIME, nullable=False),
    sa.ForeignKeyConstraint(
        ["proposal_id", "revision"],
        ["operator_proposal_revisions.proposal_id", "operator_proposal_revisions.revision"],
        name="fk_operator_review_revision",
    ),
    sa.CheckConstraint(
        "event_type IN ('SUBMITTED','CHANGES_REQUESTED','APPROVED','REJECTED')",
        name="ck_operator_review_event_type",
    ),
    sa.CheckConstraint(
        "actor_kind IN ('PROJECT','MAINTAINER')", name="ck_operator_review_actor_kind"
    ),
)
operator_deployment_receipts = table(
    "operator_deployment_receipts",
    sa.Column("receipt_id", ID, primary_key=True),
    sa.Column("proposal_id", ID, sa.ForeignKey("operator_proposals.proposal_id"), nullable=False),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("materials_digest", sa.String(71), nullable=False),
    sa.Column("release_digest", sa.String(71), nullable=False),
    sa.Column("operator_catalog_digest", sa.String(71), nullable=False),
    sa.Column("operator_id", ID, nullable=False),
    sa.Column("version_id", ID, nullable=False),
    sa.Column("operator_digest", sa.String(71), nullable=False),
    sa.Column("implementation_id", ID, nullable=False),
    sa.Column("implementation_digest", sa.String(71), nullable=False),
    sa.Column("image_digest", sa.String(71), nullable=False),
    sa.Column("source_digest", sa.String(71), nullable=False),
    sa.Column("tests_digest", sa.String(71), nullable=False),
    sa.Column("maintainer_id", ID, nullable=False),
    sa.Column("evidence_digest", sa.String(71), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.ForeignKeyConstraint(
        ["proposal_id", "revision"],
        ["operator_proposal_revisions.proposal_id", "operator_proposal_revisions.revision"],
        name="fk_operator_deployment_revision",
    ),
    sa.UniqueConstraint(
        "proposal_id",
        "revision",
        "release_digest",
        "evidence_digest",
        name="uq_operator_deployment_evidence",
    ),
)
sa.Index(
    "ix_operator_proposals_page",
    operator_proposals.c.project_id,
    operator_proposals.c.proposal_id,
)
sa.Index(
    "ix_operator_proposal_revisions_page",
    operator_proposal_revisions.c.proposal_id,
    operator_proposal_revisions.c.revision,
)
sa.Index(
    "ix_operator_review_events_history",
    operator_review_events.c.proposal_id,
    operator_review_events.c.created_at,
    operator_review_events.c.event_id,
)
sa.Index(
    "ix_operator_deployment_receipts_history",
    operator_deployment_receipts.c.proposal_id,
    operator_deployment_receipts.c.created_at,
    operator_deployment_receipts.c.receipt_id,
)
workflow_envelopes = table(
    "workflow_envelopes",
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), primary_key=True),
    sa.Column("amount", sa.Integer, nullable=False),
    sa.Column("released", sa.Boolean, nullable=False, server_default=sa.false()),
)
workflow_nodes = table(
    "workflow_nodes",
    sa.Column("node_id", ID, primary_key=True),
    sa.Column("action_id", ID, sa.ForeignKey("workflow_envelopes.action_id"), nullable=False),
    sa.Column("request", JSONB, nullable=False),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("slot_acquired", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("executed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("stopped", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("revision", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("observation", JSONB),
)

# The mutable switch is deliberately separate from immutable binding revisions.
service_bindings = table(
    "service_bindings",
    sa.Column("service_binding_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), primary_key=True),
    sa.Column("current_revision", sa.Integer, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("created_at", TIME, nullable=False, server_default=sa.func.now()),
)
service_binding_revisions = table(
    "service_binding_revisions",
    sa.Column("project_id", ID, primary_key=True),
    sa.Column("service_binding_id", ID, primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("protocol", sa.String(64), nullable=False),
    sa.Column("endpoint", sa.String(512), nullable=False),
    sa.Column("credential_ref", ID, nullable=False),
    sa.Column("tls_ca_ref", ID),
    sa.Column("targets", JSONB, nullable=False),
    sa.Column("limits", JSONB, nullable=False),
    sa.Column("binding_digest", sa.String(71), nullable=False),
    sa.Column("created_at", TIME, nullable=False, server_default=sa.func.now()),
    sa.ForeignKeyConstraint(
        ["project_id", "service_binding_id"],
        ["service_bindings.project_id", "service_bindings.service_binding_id"],
    ),
)
sa.Index(
    "service_bindings_project", service_bindings.c.project_id, service_bindings.c.service_binding_id
)
sa.Index("workflow_packages_page", workflow_packages.c.project_id, workflow_packages.c.package_id)
sa.Index(
    "workflow_packages_claim",
    workflow_packages.c.package_id,
    postgresql_where=workflow_packages.c.status.in_(["QUEUED", "VALIDATING"]),
)

# P5.2 immutable research records. Lifecycle views are derived rather than
# rewriting saved analysis or claim revisions.
policy_decisions = table(
    "policy_decisions",
    sa.Column("decision_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("policy_id", ID, nullable=False),
    sa.Column("policy_version", sa.String(64), nullable=False),
    sa.Column("policy_digest", sa.String(71), nullable=False),
    sa.Column("input_digest", sa.String(71), nullable=False),
    sa.Column("facts_used", JSONB, nullable=False),
    sa.Column("result", sa.String(24), nullable=False),
    sa.Column("reasons", JSONB, nullable=False),
    sa.Column("evaluated_at", TIME, nullable=False),
    sa.CheckConstraint(
        "result IN ('APPLICABLE','MISSING','CONFLICT','NOT_APPLICABLE')",
        name="ck_policy_result",
    ),
)
analysis_revisions = table(
    "analysis_revisions",
    sa.Column("revision_id", ID, primary_key=True),
    sa.Column("analysis_id", ID, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("supersedes_revision_id", ID, sa.ForeignKey("analysis_revisions.revision_id")),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("content_digest", sa.String(71), nullable=False),
    sa.Column(
        "policy_decision_id", ID, sa.ForeignKey("policy_decisions.decision_id"), nullable=False
    ),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint("analysis_id", "revision", name="uq_analysis_revision"),
    sa.CheckConstraint("revision > 0 AND task_spec_revision > 0", name="ck_analysis_revision"),
)
analysis_registrations = table(
    "analysis_registrations",
    sa.Column("registration_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("analysis_id", ID, nullable=False),
    sa.Column(
        "revision_id",
        ID,
        sa.ForeignKey("analysis_revisions.revision_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("observation_watermark", JSONB, nullable=False),
    sa.Column("watermark_digest", sa.String(71), nullable=False),
    sa.Column(
        "policy_decision_id", ID, sa.ForeignKey("policy_decisions.decision_id"), nullable=False
    ),
    sa.Column("registered_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("registered_at", TIME, nullable=False),
)
claim_revisions = table(
    "claim_revisions",
    sa.Column("claim_revision_id", ID, primary_key=True),
    sa.Column("claim_id", ID, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column(
        "analysis_registration_id",
        ID,
        sa.ForeignKey("analysis_registrations.registration_id"),
        nullable=False,
    ),
    sa.Column(
        "supersedes_claim_revision_id", ID, sa.ForeignKey("claim_revisions.claim_revision_id")
    ),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("content_digest", sa.String(71), nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint("claim_id", "revision", name="uq_claim_revision"),
    sa.CheckConstraint("revision > 0 AND task_spec_revision > 0", name="ck_claim_revision"),
)
provenance_relations = table(
    "provenance_relations",
    sa.Column("relation_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("relation", sa.String(32), nullable=False),
    sa.Column("provenance_kind", sa.String(32), nullable=False),
    sa.Column("subject", JSONB, nullable=False),
    sa.Column("object", JSONB, nullable=False),
    sa.Column("activity_execution_id", ID),
    sa.Column("agent_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("role", ID, nullable=False),
    sa.Column("recorded_at", TIME, nullable=False),
    sa.Column("relation_digest", sa.String(71), nullable=False, unique=True),
    sa.CheckConstraint(
        "relation IN ('USED','GENERATED','DERIVED_FROM','CITED',"
        "'SUPPORTED_BY','CHECKED_BY','INVALIDATED_BY')",
        name="ck_provenance_relation",
    ),
    sa.CheckConstraint(
        "provenance_kind IN ('DECLARED_PROVENANCE','OBSERVED_PROVENANCE')",
        name="ck_provenance_kind",
    ),
)
sa.Index("ix_policy_task", policy_decisions.c.task_id, policy_decisions.c.decision_id)
sa.Index(
    "ix_analysis_task",
    analysis_revisions.c.task_id,
    analysis_revisions.c.analysis_id,
    analysis_revisions.c.revision,
)
sa.Index(
    "ix_registration_task",
    analysis_registrations.c.task_id,
    analysis_registrations.c.registration_id,
)
sa.Index(
    "ix_claim_task",
    claim_revisions.c.task_id,
    claim_revisions.c.claim_id,
    claim_revisions.c.revision,
)
sa.Index("ix_provenance_task", provenance_relations.c.task_id, provenance_relations.c.relation_id)

# P6.1 Platform-owned Agent ledger. Framework checkpoints are kept in a
# different database; these records remain sufficient to fence and rebuild a
# runtime epoch without treating framework state as a business fact.
agent_runs = table(
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
agent_plan_revisions = table(
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
agent_context_manifests = table(
    "agent_context_manifests",
    sa.Column("manifest_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("manifest", JSONB, nullable=False),
    sa.Column("manifest_digest", sa.String(71), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint("agent_run_id", "manifest_digest", name="uq_agent_context_manifest_digest"),
)
agent_decision_frames = table(
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
agent_proposals = table(
    "agent_proposals",
    sa.Column("proposal_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column(
        "frame_id", ID, sa.ForeignKey("agent_decision_frames.frame_id"), nullable=False, unique=True
    ),
    sa.Column("epoch", sa.Integer, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("proposal", JSONB, nullable=False),
    sa.Column("proposal_digest", sa.String(71), nullable=False),
    sa.Column("accepted_at", TIME, nullable=False),
)
agent_questions = table(
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
agent_answers = table(
    "agent_answers",
    sa.Column("answer_id", ID, primary_key=True),
    sa.Column(
        "question_id", ID, sa.ForeignKey("agent_questions.question_id"), nullable=False, unique=True
    ),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("answer", JSONB, nullable=False),
    sa.Column("answer_digest", sa.String(71), nullable=False),
    sa.Column("answered_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("answered_at", TIME, nullable=False),
)
agent_events = table(
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
    sa.UniqueConstraint("source", "stream_id", "source_revision", name="uq_agent_event_revision"),
)
agent_inbox = table(
    "agent_inbox",
    sa.Column("source", sa.String(32), primary_key=True),
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("event_digest", sa.String(71), nullable=False),
    sa.Column("observed_at", TIME, nullable=False),
)
agent_wakeups = table(
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
    sa.CheckConstraint("state IN ('PENDING','CONSUMED','CANCELLED')", name="ck_agent_wakeup_state"),
)
agent_outbox = table(
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
agent_leases = table(
    "agent_leases",
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
    sa.Column("owner", ID, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("lease_until", TIME, nullable=False),
    sa.Column("acquired_at", TIME, nullable=False),
    sa.CheckConstraint("generation > 0", name="ck_agent_lease_generation"),
)
sa.Index("ix_agent_runs_ready", agent_runs.c.status, agent_runs.c.updated_at)
sa.Index(
    "ix_agent_frames_run", agent_decision_frames.c.agent_run_id, agent_decision_frames.c.sequence
)
sa.Index("ix_agent_events_run", agent_events.c.agent_run_id, agent_events.c.observed_at)
sa.Index(
    "ix_agent_wakeups_pending",
    agent_wakeups.c.agent_run_id,
    agent_wakeups.c.revision,
    postgresql_where=agent_wakeups.c.state == "PENDING",
)
sa.Index(
    "ix_agent_outbox_ready",
    agent_outbox.c.available_at,
    postgresql_where=agent_outbox.c.done.is_(False),
)

# P6.2 model calls are accounted separately from scientific execution.  A
# reservation is committed before provider I/O and remains held while either
# the result or its usage is unknown.
agent_model_budgets = table(
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
)
agent_model_calls = table(
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
)
agent_model_reservations = table(
    "agent_model_reservations",
    sa.Column(
        "model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id"), primary_key=True
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
)
agent_model_observations = table(
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
model_provider_reconciliation = table(
    "model_provider_reconciliation",
    sa.Column(
        "model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id"), primary_key=True
    ),
    sa.Column("next_attempt_at", TIME, nullable=False),
    sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_error_code", sa.String(64)),
    sa.Column("owner", ID),
    sa.Column("lease_until", TIME),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
)
sa.Index(
    "ix_agent_model_calls_run", agent_model_calls.c.agent_run_id, agent_model_calls.c.created_at
)
sa.Index("ix_model_reconciliation_ready", model_provider_reconciliation.c.next_attempt_at)

# P6.3 read tools are separated from free-form Agent events.  A request is
# committed before service I/O; the single observation is immutable and is the
# only content admitted to a later model frame.
agent_tool_requests = table(
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
agent_tool_observations = table(
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
sa.Index(
    "ix_agent_tool_requests_run",
    agent_tool_requests.c.agent_run_id,
    agent_tool_requests.c.created_at,
)

# P6.4 joins an accepted Agent tool request to the business identity returned
# by the existing application use case. The link is recovery evidence; the
# Action, Deliverable, and Acceptance tables remain authoritative.
agent_action_links = table(
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
        "operation IN ('SUBMIT_OPERATOR','SUBMIT_WORKFLOW','REGISTER_DELIVERABLE',"
        "'REQUEST_ACCEPTANCE','CREATE_ANALYSIS','REGISTER_ANALYSIS','CREATE_CLAIM')",
        name="ck_agent_action_link_operation",
    ),
)
sa.Index(
    "ix_agent_action_links_run", agent_action_links.c.agent_run_id, agent_action_links.c.created_at
)

# P6.5 freezes what a WAIT proposal is waiting for. Source events and the
# framework checkpoint are hints; these rows plus the authoritative business
# objects decide whether another model turn is allowed.
agent_wait_dependencies = table(
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
sa.Index(
    "ix_agent_wait_active",
    agent_wait_dependencies.c.agent_run_id,
    agent_wait_dependencies.c.state,
)

agent_source_watermarks = table(
    "agent_source_watermarks",
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
    sa.Column("source", sa.String(32), primary_key=True),
    sa.Column("stream_id", ID, primary_key=True),
    sa.Column("latest_revision", sa.BigInteger, nullable=False),
    sa.Column("latest_event_digest", sa.String(71), nullable=False),
    sa.Column("updated_at", TIME, nullable=False),
)

agent_runtime_recoveries = table(
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

agent_reconciliation_timers = table(
    "agent_reconciliation_timers",
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
    sa.Column("next_attempt_at", TIME, nullable=False),
    sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_category", sa.String(64)),
    sa.Column("updated_at", TIME, nullable=False),
)

# P6.7 records every autonomous delivery/repair branch. A new model frame may
# propose a different request, but it cannot erase earlier failures or exceed
# the deterministic branch limit.
agent_delivery_attempts = table(
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
    sa.UniqueConstraint("agent_run_id", "branch_key", "ordinal", name="uq_agent_branch_ordinal"),
    sa.CheckConstraint(
        "outcome IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN','STOPPED')",
        name="ck_agent_delivery_outcome",
    ),
)
sa.Index(
    "ix_agent_delivery_branch",
    agent_delivery_attempts.c.agent_run_id,
    agent_delivery_attempts.c.branch_key,
)
