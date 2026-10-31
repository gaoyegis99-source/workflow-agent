"""Bind V7 Principals to stable trusted-proxy subjects.

Revision ID: 0024_principal_external_subject
Revises: 0023_workflow_release_lifecycle
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_principal_external_subject"
down_revision = "0023_workflow_release_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("principals", sa.Column("external_subject", sa.String(128), nullable=True))
    op.create_unique_constraint(
        "uq_principals_external_subject", "principals", ["external_subject"]
    )
    op.create_table(
        "principal_identity_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column(
            "principal_id", sa.String(128), sa.ForeignKey("principals.principal_id"), nullable=False
        ),
        sa.Column("previous_subject", sa.String(128), nullable=True),
        sa.Column("bound_subject", sa.String(128), nullable=False),
        sa.Column("reviewer", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    op.drop_table("principal_identity_events")
    op.drop_constraint("uq_principals_external_subject", "principals", type_="unique")
    op.drop_column("principals", "external_subject")
