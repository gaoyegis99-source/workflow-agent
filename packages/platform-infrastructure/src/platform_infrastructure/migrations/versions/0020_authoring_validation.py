"""Add fenced asynchronous authoring validation.

Revision ID: 0020_authoring_validation
Revises: 0019_authoring_drafts
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_authoring_validation"
down_revision = "0019_authoring_drafts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "authoring_validations",
        sa.Column("validation_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("projects.project_id"),
            nullable=False,
        ),
        sa.Column("definition_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_digest", sa.String(71), nullable=False),
        sa.Column("source", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context_digest", sa.String(71), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("issues", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("owner", sa.String(128)),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("principals.principal_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
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
    op.create_index(
        "ix_authoring_validations_project",
        "authoring_validations",
        ["project_id", "validation_id"],
    )
    op.create_index(
        "ix_authoring_validations_queue",
        "authoring_validations",
        ["status", "lease_until", "created_at"],
    )


def downgrade():
    op.drop_index("ix_authoring_validations_queue", table_name="authoring_validations")
    op.drop_index("ix_authoring_validations_project", table_name="authoring_validations")
    op.drop_table("authoring_validations")
