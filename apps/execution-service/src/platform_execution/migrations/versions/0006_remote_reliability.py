"""Persist the remote observation history used for event-level deduplication."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_remote_reliability"
down_revision = "0005_remote_calls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "remote_observations",
        sa.Column(
            "action_id", sa.String(128), sa.ForeignKey("remote_calls.action_id"), primary_key=True
        ),
        sa.Column("event_id", sa.String(256), primary_key=True),
        sa.Column("revision", sa.BigInteger, nullable=False),
        sa.Column("observation_digest", sa.String(71), nullable=False),
        sa.Column("observation", JSONB, nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
    )


def downgrade():
    op.drop_table("remote_observations")
