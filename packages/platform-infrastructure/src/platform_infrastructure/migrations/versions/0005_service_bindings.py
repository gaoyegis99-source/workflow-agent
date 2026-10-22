"""Immutable remote service binding revisions and separate enable switch."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_service_bindings"
down_revision = "0004_workflows"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "service_bindings",
        sa.Column("service_binding_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column("current_revision", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("current_revision > 0", name="service_binding_revision_positive"),
    )
    op.create_table(
        "service_binding_revisions",
        sa.Column(
            "service_binding_id",
            sa.String(128),
            sa.ForeignKey("service_bindings.service_binding_id"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("protocol", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("credential_ref", sa.String(128), nullable=False),
        sa.Column("targets", JSONB, nullable=False),
        sa.Column("limits", JSONB, nullable=False),
        sa.Column("binding_digest", sa.String(71), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("revision > 0", name="service_binding_revision_item_positive"),
    )
    op.create_index(
        "service_bindings_project", "service_bindings", ["project_id", "service_binding_id"]
    )


def downgrade():
    op.drop_table("service_binding_revisions")
    op.drop_table("service_bindings")
