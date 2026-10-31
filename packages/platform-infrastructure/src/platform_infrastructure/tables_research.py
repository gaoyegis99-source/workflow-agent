"""Immutable research, policy, and provenance table declarations."""

from .tables_shared import ID, JSONB, TIME, sa, table

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
