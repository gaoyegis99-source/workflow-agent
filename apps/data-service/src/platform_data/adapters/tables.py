import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()
ID = sa.String(128)
uploads = sa.Table(
    "uploads",
    metadata,
    sa.Column("upload_id", ID, primary_key=True),
    sa.Column("project_id", ID, nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("runtime", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("owner", ID),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
)
assets = sa.Table(
    "assets",
    metadata,
    sa.Column("asset_id", ID, primary_key=True),
    sa.Column("version_id", ID, primary_key=True),
    sa.Column("project_id", ID, nullable=False),
    sa.Column("manifest", JSONB, nullable=False),
    sa.Column("manifest_digest", sa.String(71), nullable=False),
)
semantics = sa.Table(
    "semantic_revisions",
    metadata,
    sa.Column("asset_id", ID, primary_key=True),
    sa.Column("version_id", ID, primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("payload", JSONB, nullable=False),
)
publications = sa.Table(
    "publications",
    metadata,
    sa.Column("execution_id", ID, primary_key=True),
    sa.Column("project_id", ID, nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("attempt_id", ID),
    sa.Column("manifest_digest", sa.String(64)),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("receipt", JSONB),
)
publication_members = sa.Table(
    "publication_members",
    metadata,
    sa.Column("execution_id", ID, primary_key=True),
    sa.Column("member_id", ID, primary_key=True),
    sa.Column("asset_ref", JSONB, nullable=False),
)
requests = sa.Table(
    "idempotency",
    metadata,
    sa.Column("scope", sa.String(512), primary_key=True),
    sa.Column("key", ID, primary_key=True),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("result", JSONB),
)
