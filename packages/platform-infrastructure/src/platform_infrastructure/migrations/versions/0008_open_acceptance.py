"""Open acceptance, human review and completion validity.

Revision ID: 0008_open_acceptance
Revises: 0007_research_policy
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_open_acceptance"
down_revision = "0007_research_policy"
branch_labels = None
depends_on = None
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "acceptance_coverage_plans",
        sa.Column("plan_id", ID, primary_key=True),
        sa.Column(
            "acceptance_id",
            ID,
            sa.ForeignKey("acceptances.acceptance_id"),
            nullable=False,
            unique=True,
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
        sa.CheckConstraint("revision > 0", name="ck_coverage_plan_revision"),
    )
    op.create_index("ix_coverage_task", "acceptance_coverage_plans", ["task_id", "acceptance_id"])
    op.create_table(
        "acceptance_coverage_items",
        sa.Column(
            "plan_id", ID, sa.ForeignKey("acceptance_coverage_plans.plan_id"), primary_key=True
        ),
        sa.Column("item_id", ID, primary_key=True),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("requirement_id", ID, nullable=False),
        sa.Column("check_type", sa.String(32), nullable=False),
        sa.Column("necessary", sa.Boolean, nullable=False),
        sa.Column("automatic", sa.Boolean, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", JSONB),
        sa.UniqueConstraint("plan_id", "ordinal", name="uq_coverage_ordinal"),
    )
    op.create_table(
        "acceptance_human_reviews",
        sa.Column("review_id", ID, primary_key=True),
        sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), nullable=False),
        sa.Column(
            "plan_id", ID, sa.ForeignKey("acceptance_coverage_plans.plan_id"), nullable=False
        ),
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
    op.create_table(
        "acceptance_evidence_refs",
        sa.Column(
            "acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), primary_key=True
        ),
        sa.Column("evidence_digest", sa.String(64), primary_key=True),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("document_id", ID, nullable=False),
        sa.Column("version_id", ID, nullable=False),
        sa.Column("evidence_ref", JSONB, nullable=False),
    )
    op.create_index(
        "ix_acceptance_evidence_version",
        "acceptance_evidence_refs",
        ["project_id", "document_id", "version_id"],
    )
    op.create_table(
        "completion_validity",
        sa.Column("task_id", ID, sa.ForeignKey("completions.task_id"), primary_key=True),
        sa.Column("validity", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("reasons", JSONB, nullable=False),
        sa.Column("updated_at", TIME, nullable=False),
    )
    op.create_table(
        "knowledge_lifecycle_cursors",
        sa.Column("project_id", ID, primary_key=True),
        sa.Column("document_id", ID, primary_key=True),
        sa.Column("version_id", ID, primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("event_id", ID, nullable=False),
    )


def downgrade():
    for name in [
        "knowledge_lifecycle_cursors",
        "completion_validity",
        "acceptance_evidence_refs",
        "acceptance_human_reviews",
        "acceptance_coverage_items",
        "acceptance_coverage_plans",
    ]:
        op.drop_table(name)
