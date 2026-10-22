"""Exact deliverable identity and selected analysis registration.

Revision ID: 0009_output_identity
Revises: 0008_open_acceptance
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_output_identity"
down_revision = "0008_open_acceptance"
branch_labels = None
depends_on = None
ID = sa.String(128)


def upgrade():
    op.add_column(
        "deliverables",
        sa.Column("identity_mode", sa.String(16), nullable=False, server_default="LEGACY"),
    )
    op.create_check_constraint(
        "ck_deliverable_identity_mode",
        "deliverables",
        "identity_mode IN ('LEGACY','EXACT')",
    )
    op.add_column(
        "acceptances",
        sa.Column(
            "analysis_registration_id",
            ID,
            sa.ForeignKey("analysis_registrations.registration_id"),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("acceptances", "analysis_registration_id")
    op.drop_constraint("ck_deliverable_identity_mode", "deliverables", type_="check")
    op.drop_column("deliverables", "identity_mode")
