"""Add immutable workflow authoring drafts.

Revision ID: 0019_authoring_drafts
Revises: 0018_agent_acceptance_identity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_authoring_drafts"
down_revision = "0018_agent_acceptance_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_definitions",
        sa.Column("definition_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("projects.project_id"),
            nullable=False,
        ),
        sa.Column("stable_key", sa.String(128), nullable=False),
        sa.Column("head_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("principals.principal_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("head_revision > 0", name="ck_workflow_definitions_head_positive"),
        sa.UniqueConstraint("project_id", "stable_key", name="uq_workflow_definitions_stable_key"),
    )
    op.create_index(
        "ix_workflow_definitions_page",
        "workflow_definitions",
        ["project_id", "definition_id"],
    )
    op.create_table(
        "workflow_revisions",
        sa.Column(
            "definition_id",
            sa.String(128),
            sa.ForeignKey("workflow_definitions.definition_id"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(128),
            sa.ForeignKey("projects.project_id"),
            nullable=False,
        ),
        sa.Column("source", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_digest", sa.String(71), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("layout_digest", sa.String(71), nullable=False),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("principals.principal_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_workflow_revisions_revision_positive"),
    )
    op.create_index(
        "ix_workflow_revisions_page",
        "workflow_revisions",
        ["definition_id", "revision"],
    )


def downgrade():
    op.drop_index("ix_workflow_revisions_page", table_name="workflow_revisions")
    op.drop_table("workflow_revisions")
    op.drop_index("ix_workflow_definitions_page", table_name="workflow_definitions")
    op.drop_table("workflow_definitions")
