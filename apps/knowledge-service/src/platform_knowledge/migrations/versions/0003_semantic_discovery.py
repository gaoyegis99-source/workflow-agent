"""Add versioned candidate-only semantic discovery indexes.

Revision ID: 0003_semantic_discovery
Revises: 0002_lifecycle_outbox
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_semantic_discovery"
down_revision = "0002_lifecycle_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_semantic_indexes",
        sa.Column("index_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("profile", JSONB, nullable=False),
        sa.Column("profile_digest", sa.String(71), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("failure_category", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "profile_digest", name="uq_knowledge_semantic_profile"),
    )
    op.create_table(
        "knowledge_semantic_vectors",
        sa.Column("index_id", sa.String(128), nullable=False),
        sa.Column("unit_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("content_digest", sa.String(71), nullable=False),
        sa.Column("lifecycle_revision", sa.Integer, nullable=False),
        sa.Column("vector", JSONB, nullable=False),
        sa.Column("valid", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["index_id"], ["knowledge_semantic_indexes.index_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("index_id", "unit_id"),
    )
    op.create_index(
        "ix_knowledge_semantic_vectors_active",
        "knowledge_semantic_vectors",
        ["project_id", "index_id", "valid"],
    )
    op.create_index(
        "ix_knowledge_semantic_vectors_version",
        "knowledge_semantic_vectors",
        ["document_id", "version_id", "valid"],
    )


def downgrade():
    op.drop_index("ix_knowledge_semantic_vectors_version", table_name="knowledge_semantic_vectors")
    op.drop_index("ix_knowledge_semantic_vectors_active", table_name="knowledge_semantic_vectors")
    op.drop_table("knowledge_semantic_vectors")
    op.drop_table("knowledge_semantic_indexes")
