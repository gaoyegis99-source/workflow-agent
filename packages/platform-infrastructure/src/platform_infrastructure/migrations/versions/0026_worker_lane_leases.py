"""Fence acceptance-worker progress with independent leases.

Revision ID: 0026_worker_lane_leases
Revises: 0025_exact_identity_only
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_worker_lane_leases"
down_revision = "0025_exact_identity_only"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("acceptances", sa.Column("owner", sa.String(128)))
    op.add_column(
        "acceptances",
        sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.add_column("acceptances", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column(
        "acceptances",
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_acceptances_worker_ready",
        "acceptances",
        ["status", "lease_until", "acceptance_id"],
    )


def downgrade():
    op.drop_index("ix_acceptances_worker_ready", table_name="acceptances")
    for name in ("attempts", "lease_until", "generation", "owner"):
        op.drop_column("acceptances", name)
