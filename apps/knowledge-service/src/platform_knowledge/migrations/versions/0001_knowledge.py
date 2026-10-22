"""Initial independent Knowledge schema.

Revision ID: 0001_knowledge
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision = "0001_knowledge"
down_revision = None
branch_labels = None
depends_on = None

ID = sa.String(128)
DIGEST = sa.String(71)


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "knowledge_imports",
        sa.Column("import_id", ID, primary_key=True),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("owner", ID),
        sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_knowledge_import_claim", "knowledge_imports", ["state", "lease_until"])
    op.create_table(
        "knowledge_documents",
        sa.Column("document_id", ID, primary_key=True),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_knowledge_documents_project", "knowledge_documents", ["project_id", "document_id"]
    )
    op.create_table(
        "knowledge_versions",
        sa.Column("document_id", ID, primary_key=True),
        sa.Column("version_id", ID, primary_key=True),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source", JSONB, nullable=False),
        sa.Column("source_asset_id", ID, nullable=False),
        sa.Column("source_version_id", ID, nullable=False),
        sa.Column("source_manifest_digest", DIGEST, nullable=False),
        sa.Column("source_digest", DIGEST, nullable=False),
        sa.Column("content_digest", DIGEST, nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("availability", sa.String(16), nullable=False),
        sa.Column("epistemic_status", sa.String(24), nullable=False),
        sa.Column("review_state", sa.String(24), nullable=False),
        sa.Column("lifecycle_revision", sa.Integer, nullable=False),
        sa.Column("unit_count", sa.Integer, nullable=False),
        sa.Column("withdrawal_reason", sa.String(500)),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "source_asset_id",
            "source_version_id",
            "source_manifest_digest",
            name="uq_knowledge_source",
        ),
    )
    op.create_index(
        "ix_knowledge_versions_project",
        "knowledge_versions",
        ["project_id", "document_id", "version_id"],
    )
    op.create_table(
        "evidence_units",
        sa.Column("unit_id", ID, primary_key=True),
        sa.Column("document_id", ID, nullable=False),
        sa.Column("version_id", ID, nullable=False),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("start_char", sa.Integer, nullable=False),
        sa.Column("end_char", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("exact_text_digest", DIGEST, nullable=False),
        sa.Column("prefix", sa.Text, nullable=False),
        sa.Column("suffix", sa.Text, nullable=False),
        sa.Column("search_vector", TSVECTOR, nullable=False),
        sa.UniqueConstraint(
            "document_id", "version_id", "start_char", "end_char", name="uq_evidence_range"
        ),
    )
    op.create_index("ix_evidence_project", "evidence_units", ["project_id", "unit_id"])
    op.execute("CREATE INDEX ix_evidence_fts ON evidence_units USING GIN (search_vector)")
    op.execute("CREATE INDEX ix_evidence_trgm ON evidence_units USING GIN (text gin_trgm_ops)")
    op.create_table(
        "evidence_read_receipts",
        sa.Column("receipt_id", ID, primary_key=True),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("document_id", ID, nullable=False),
        sa.Column("version_id", ID, nullable=False),
        sa.Column("unit_id", ID, nullable=False),
        sa.Column("actor_id", ID, nullable=False),
        sa.Column("lifecycle_revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "knowledge_lifecycle_events",
        sa.Column("event_id", ID, primary_key=True),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("document_id", ID, nullable=False),
        sa.Column("version_id", ID, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "version_id", "revision", name="uq_lifecycle_revision"),
    )
    op.create_table(
        "knowledge_outbox",
        sa.Column("event_id", ID, primary_key=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "knowledge_idempotency",
        sa.Column("scope", sa.String(512), primary_key=True),
        sa.Column("key", ID, primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("result", JSONB),
    )


def downgrade():
    for name in [
        "knowledge_idempotency",
        "knowledge_outbox",
        "knowledge_lifecycle_events",
        "evidence_read_receipts",
        "evidence_units",
        "knowledge_versions",
        "knowledge_documents",
        "knowledge_imports",
    ]:
        op.drop_table(name)
