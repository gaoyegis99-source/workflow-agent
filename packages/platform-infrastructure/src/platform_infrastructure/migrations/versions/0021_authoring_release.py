"""Add immutable authoring candidates, trials and releases.

Revision ID: 0021_authoring_release
Revises: 0020_authoring_validation
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_authoring_release"
down_revision = "0020_authoring_validation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_candidates",
        sa.Column("candidate_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column("definition_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "validation_id",
            sa.String(128),
            sa.ForeignKey("authoring_validations.validation_id"),
            nullable=False,
        ),
        sa.Column(
            "package_id",
            sa.String(128),
            sa.ForeignKey("workflow_packages.package_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("source_digest", sa.String(71), nullable=False),
        sa.Column("expected_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_manifest_digest", sa.String(71), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context_digest", sa.String(71), nullable=False),
        sa.Column(
            "created_by", sa.String(128), sa.ForeignKey("principals.principal_id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["definition_id", "revision"],
            ["workflow_revisions.definition_id", "workflow_revisions.revision"],
            name="fk_workflow_candidate_revision",
        ),
    )
    op.create_index(
        "ix_workflow_candidates_page",
        "workflow_candidates",
        ["project_id", "definition_id", "candidate_id"],
    )

    op.create_table(
        "workflow_trials",
        sa.Column("trial_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column(
            "definition_id",
            sa.String(128),
            sa.ForeignKey("workflow_definitions.definition_id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(128),
            sa.ForeignKey("workflow_candidates.candidate_id"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(128), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column(
            "action_id",
            sa.String(128),
            sa.ForeignKey("actions.action_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inputs_digest", sa.String(71), nullable=False),
        sa.Column("trial_case", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trial_case_digest", sa.String(71), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context_digest", sa.String(71), nullable=False),
        sa.Column("case_verdict", sa.String(16), nullable=False),
        sa.Column("case_result", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("case_result_digest", sa.String(71)),
        sa.Column("error_category", sa.String(128)),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("owner", sa.String(128)),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_by", sa.String(128), sa.ForeignKey("principals.principal_id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "case_verdict IN ('PENDING','PASS','FAIL','ERROR')",
            name="ck_workflow_trial_verdict",
        ),
    )
    op.create_index(
        "ix_workflow_trials_page",
        "workflow_trials",
        ["project_id", "definition_id", "trial_id"],
    )
    op.create_index(
        "ix_workflow_trials_queue",
        "workflow_trials",
        ["case_verdict", "next_retry_at", "lease_until", "created_at"],
    )

    op.create_table(
        "workflow_releases",
        sa.Column("release_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column(
            "definition_id",
            sa.String(128),
            sa.ForeignKey("workflow_definitions.definition_id"),
            nullable=False,
        ),
        sa.Column("version_label", sa.String(64), nullable=False),
        sa.Column("version_sort", postgresql.ARRAY(sa.String(96)), nullable=False),
        sa.Column(
            "candidate_id",
            sa.String(128),
            sa.ForeignKey("workflow_candidates.candidate_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_digest", sa.String(71), nullable=False),
        sa.Column("package_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context_digest", sa.String(71), nullable=False),
        sa.Column(
            "created_by", sa.String(128), sa.ForeignKey("principals.principal_id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "definition_id", "version_label", name="uq_workflow_release_version"
        ),
    )
    op.create_index(
        "ix_workflow_releases_page",
        "workflow_releases",
        ["project_id", "definition_id", "version_sort", "release_id"],
    )
    op.create_table(
        "workflow_release_trials",
        sa.Column(
            "release_id",
            sa.String(128),
            sa.ForeignKey("workflow_releases.release_id"),
            primary_key=True,
        ),
        sa.Column(
            "trial_id", sa.String(128), sa.ForeignKey("workflow_trials.trial_id"), primary_key=True
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("trial_case_digest", sa.String(71), nullable=False),
        sa.Column("case_result_digest", sa.String(71), nullable=False),
        sa.UniqueConstraint("release_id", "ordinal", name="uq_workflow_release_trial_ordinal"),
    )


def downgrade():
    op.drop_table("workflow_release_trials")
    op.drop_index("ix_workflow_releases_page", table_name="workflow_releases")
    op.drop_table("workflow_releases")
    op.drop_index("ix_workflow_trials_queue", table_name="workflow_trials")
    op.drop_index("ix_workflow_trials_page", table_name="workflow_trials")
    op.drop_table("workflow_trials")
    op.drop_index("ix_workflow_candidates_page", table_name="workflow_candidates")
    op.drop_table("workflow_candidates")
