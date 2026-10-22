"""Lease and acknowledge Knowledge lifecycle delivery.

Revision ID: 0002_lifecycle_outbox
Revises: 0001_knowledge
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_lifecycle_outbox"
down_revision = "0001_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_outbox", sa.Column("done", sa.Boolean, nullable=False, server_default=sa.false())
    )
    op.add_column("knowledge_outbox", sa.Column("owner", sa.String(128)))
    op.add_column(
        "knowledge_outbox",
        sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.add_column("knowledge_outbox", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column(
        "knowledge_outbox", sa.Column("attempts", sa.Integer, nullable=False, server_default="0")
    )
    op.create_index(
        "ix_knowledge_outbox_ready", "knowledge_outbox", ["done", "lease_until", "created_at"]
    )


def downgrade():
    op.drop_index("ix_knowledge_outbox_ready", table_name="knowledge_outbox")
    for name in ["attempts", "lease_until", "generation", "owner", "done"]:
        op.drop_column("knowledge_outbox", name)
