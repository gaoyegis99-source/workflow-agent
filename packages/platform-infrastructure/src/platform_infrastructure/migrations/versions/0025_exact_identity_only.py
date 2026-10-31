"""Retire legacy deliverable identities after the audited data migration.

Revision ID: 0025_exact_identity_only
Revises: 0024_principal_external_subject
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_exact_identity_only"
down_revision = "0024_principal_external_subject"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    legacy = connection.scalar(
        sa.text("SELECT count(*) FROM deliverables WHERE identity_mode <> 'EXACT'")
    )
    if legacy:
        raise RuntimeError(
            "LEGACY_IDENTITY_REMAINS: run bin/ops migrate exact-identity before schema upgrade"
        )
    op.drop_constraint("ck_deliverable_identity_mode", "deliverables", type_="check")
    op.alter_column(
        "deliverables",
        "identity_mode",
        existing_type=sa.String(16),
        nullable=False,
        server_default="EXACT",
    )
    op.create_check_constraint(
        "ck_deliverable_identity_mode",
        "deliverables",
        "identity_mode = 'EXACT'",
    )


def downgrade():
    op.drop_constraint("ck_deliverable_identity_mode", "deliverables", type_="check")
    op.alter_column(
        "deliverables",
        "identity_mode",
        existing_type=sa.String(16),
        nullable=False,
        server_default="LEGACY",
    )
    op.create_check_constraint(
        "ck_deliverable_identity_mode",
        "deliverables",
        "identity_mode IN ('LEGACY','EXACT')",
    )
