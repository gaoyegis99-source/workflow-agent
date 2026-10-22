"""Add immutable operator proposal and deployment evidence records.

Revision ID: 0022_operator_proposals
Revises: 0021_authoring_release
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_operator_proposals"
down_revision = "0021_authoring_release"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operator_proposals",
        sa.Column("proposal_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column("stable_key", sa.String(128), nullable=False),
        sa.Column("head_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_by", sa.String(128), sa.ForeignKey("principals.principal_id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("head_revision > 0", name="ck_operator_proposals_head_positive"),
        sa.UniqueConstraint("project_id", "stable_key", name="uq_operator_proposals_stable_key"),
    )
    op.create_index(
        "ix_operator_proposals_page", "operator_proposals", ["project_id", "proposal_id"]
    )
    op.create_table(
        "operator_proposal_revisions",
        sa.Column(
            "proposal_id",
            sa.String(128),
            sa.ForeignKey("operator_proposals.proposal_id"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column("materials", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("materials_digest", sa.String(71), nullable=False),
        sa.Column(
            "created_by", sa.String(128), sa.ForeignKey("principals.principal_id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_operator_proposal_revision_positive"),
    )
    op.create_index(
        "ix_operator_proposal_revisions_page",
        "operator_proposal_revisions",
        ["proposal_id", "revision"],
    )
    op.create_table(
        "operator_review_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(128),
            sa.ForeignKey("operator_proposals.proposal_id"),
            nullable=False,
        ),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("materials_digest", sa.String(71), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(4000), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("evidence_digest", sa.String(71)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
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
    op.create_index(
        "ix_operator_review_events_history",
        "operator_review_events",
        ["proposal_id", "created_at", "event_id"],
    )
    op.create_table(
        "operator_deployment_receipts",
        sa.Column("receipt_id", sa.String(128), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(128),
            sa.ForeignKey("operator_proposals.proposal_id"),
            nullable=False,
        ),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("materials_digest", sa.String(71), nullable=False),
        sa.Column("release_digest", sa.String(71), nullable=False),
        sa.Column("operator_catalog_digest", sa.String(71), nullable=False),
        sa.Column("operator_id", sa.String(128), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("operator_digest", sa.String(71), nullable=False),
        sa.Column("implementation_id", sa.String(128), nullable=False),
        sa.Column("implementation_digest", sa.String(71), nullable=False),
        sa.Column("image_digest", sa.String(71), nullable=False),
        sa.Column("source_digest", sa.String(71), nullable=False),
        sa.Column("tests_digest", sa.String(71), nullable=False),
        sa.Column("maintainer_id", sa.String(128), nullable=False),
        sa.Column("evidence_digest", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
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
    op.create_index(
        "ix_operator_deployment_receipts_history",
        "operator_deployment_receipts",
        ["proposal_id", "created_at", "receipt_id"],
    )


def downgrade():
    op.drop_index(
        "ix_operator_deployment_receipts_history", table_name="operator_deployment_receipts"
    )
    op.drop_table("operator_deployment_receipts")
    op.drop_index("ix_operator_review_events_history", table_name="operator_review_events")
    op.drop_table("operator_review_events")
    op.drop_index("ix_operator_proposal_revisions_page", table_name="operator_proposal_revisions")
    op.drop_table("operator_proposal_revisions")
    op.drop_index("ix_operator_proposals_page", table_name="operator_proposals")
    op.drop_table("operator_proposals")
