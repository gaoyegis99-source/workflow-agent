"""Frozen packages and workflow node budget ledger; additive to P3."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_workflows"
down_revision = "0003_p3_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_packages",
        sa.Column("package_id", sa.String(128), primary_key=True),
        sa.Column(
            "project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("compiled", JSONB),
        sa.Column("error", JSONB),
        sa.Column("owner", sa.String(128)),
        sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "workflow_envelopes",
        sa.Column(
            "action_id", sa.String(128), sa.ForeignKey("actions.action_id"), primary_key=True
        ),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("released", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_table(
        "workflow_nodes",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column(
            "action_id",
            sa.String(128),
            sa.ForeignKey("workflow_envelopes.action_id"),
            nullable=False,
        ),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("slot_acquired", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("executed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("stopped", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("observation", JSONB),
    )
    op.create_index("workflow_packages_page", "workflow_packages", ["project_id", "package_id"])
    op.create_index(
        "workflow_packages_claim",
        "workflow_packages",
        ["package_id"],
        postgresql_where=sa.text("status IN ('QUEUED', 'VALIDATING')"),
    )


def downgrade():
    for name in ("workflow_nodes", "workflow_envelopes", "workflow_packages"):
        op.drop_table(name)
