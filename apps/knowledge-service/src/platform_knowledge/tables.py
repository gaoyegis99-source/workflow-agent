import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

metadata = sa.MetaData()
ID = sa.String(128)
DIGEST = sa.String(71)

imports = sa.Table(
    "knowledge_imports",
    metadata,
    sa.Column("import_id", ID, primary_key=True),
    sa.Column("project_id", ID, nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("owner", ID),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
)
documents = sa.Table(
    "knowledge_documents",
    metadata,
    sa.Column("document_id", ID, primary_key=True),
    sa.Column("project_id", ID, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
versions = sa.Table(
    "knowledge_versions",
    metadata,
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
units = sa.Table(
    "evidence_units",
    metadata,
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
read_receipts = sa.Table(
    "evidence_read_receipts",
    metadata,
    sa.Column("receipt_id", ID, primary_key=True),
    sa.Column("project_id", ID, nullable=False),
    sa.Column("document_id", ID, nullable=False),
    sa.Column("version_id", ID, nullable=False),
    sa.Column("unit_id", ID, nullable=False),
    sa.Column("actor_id", ID, nullable=False),
    sa.Column("lifecycle_revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
lifecycle_events = sa.Table(
    "knowledge_lifecycle_events",
    metadata,
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
outbox = sa.Table(
    "knowledge_outbox",
    metadata,
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("done", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("owner", ID),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
)
requests = sa.Table(
    "knowledge_idempotency",
    metadata,
    sa.Column("scope", sa.String(512), primary_key=True),
    sa.Column("key", ID, primary_key=True),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("result", JSONB),
)
