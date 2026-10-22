"""Immutable research protocols, claims, provenance and policy decisions.

Revision ID: 0007_research_policy
Revises: 0006_service_binding_scope
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_research_policy"
down_revision = "0006_service_binding_scope"
branch_labels = None
depends_on = None

ID = sa.String(128)
TIME = sa.DateTime(timezone=True)
DIGEST = sa.String(71)


def upgrade():
    op.create_table(
        "policy_decisions",
        sa.Column("decision_id", ID, primary_key=True),
        sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("policy_id", ID, nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_digest", DIGEST, nullable=False),
        sa.Column("input_digest", DIGEST, nullable=False),
        sa.Column("facts_used", JSONB, nullable=False),
        sa.Column("result", sa.String(24), nullable=False),
        sa.Column("reasons", JSONB, nullable=False),
        sa.Column("evaluated_at", TIME, nullable=False),
        sa.CheckConstraint(
            "result IN ('APPLICABLE','MISSING','CONFLICT','NOT_APPLICABLE')",
            name="ck_policy_result",
        ),
    )
    op.create_index("ix_policy_task", "policy_decisions", ["task_id", "decision_id"])
    op.create_table(
        "analysis_revisions",
        sa.Column("revision_id", ID, primary_key=True),
        sa.Column("analysis_id", ID, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("task_spec_revision", sa.Integer, nullable=False),
        sa.Column("supersedes_revision_id", ID, sa.ForeignKey("analysis_revisions.revision_id")),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("content_digest", DIGEST, nullable=False),
        sa.Column(
            "policy_decision_id", ID, sa.ForeignKey("policy_decisions.decision_id"), nullable=False
        ),
        sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.UniqueConstraint("analysis_id", "revision", name="uq_analysis_revision"),
        sa.CheckConstraint("revision > 0 AND task_spec_revision > 0", name="ck_analysis_revision"),
    )
    op.create_index(
        "ix_analysis_task", "analysis_revisions", ["task_id", "analysis_id", "revision"]
    )
    op.create_table(
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
        sa.Column("watermark_digest", DIGEST, nullable=False),
        sa.Column(
            "policy_decision_id", ID, sa.ForeignKey("policy_decisions.decision_id"), nullable=False
        ),
        sa.Column("registered_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
        sa.Column("registered_at", TIME, nullable=False),
    )
    op.create_index(
        "ix_registration_task", "analysis_registrations", ["task_id", "registration_id"]
    )
    op.create_table(
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
        sa.Column("content_digest", DIGEST, nullable=False),
        sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.UniqueConstraint("claim_id", "revision", name="uq_claim_revision"),
        sa.CheckConstraint("revision > 0 AND task_spec_revision > 0", name="ck_claim_revision"),
    )
    op.create_index("ix_claim_task", "claim_revisions", ["task_id", "claim_id", "revision"])
    op.create_table(
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
        sa.Column("relation_digest", DIGEST, nullable=False, unique=True),
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
    op.create_index("ix_provenance_task", "provenance_relations", ["task_id", "relation_id"])


def downgrade():
    op.drop_table("provenance_relations")
    op.drop_table("claim_revisions")
    op.drop_table("analysis_registrations")
    op.drop_table("analysis_revisions")
    op.drop_table("policy_decisions")
