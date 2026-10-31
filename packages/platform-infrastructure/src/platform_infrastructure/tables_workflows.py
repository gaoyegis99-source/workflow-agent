"""Workflow package, authoring, release, and service-binding declarations."""

from .tables_shared import ID, JSONB, TIME, sa, table

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
