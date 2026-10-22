import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()
executions = sa.Table(
    "executions",
    metadata,
    sa.Column("action_id", sa.String(128), primary_key=True),
    sa.Column("execution_id", sa.String(128), nullable=False, unique=True),
    sa.Column("project_id", sa.String(128), nullable=False),
    sa.Column("task_id", sa.String(128), nullable=False),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("executed", sa.Boolean, nullable=False),
    sa.Column("cancel_requested", sa.Boolean, nullable=False),
    sa.Column("attempt_id", sa.String(128), nullable=False),
    sa.Column("container_id", sa.String(128)),
    sa.Column("runtime", JSONB, nullable=False),
    sa.Column("owner", sa.String(128)),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column(
        "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)
