"""Scope service-binding identities by project and add the v2 CA reference."""

import sqlalchemy as sa
from alembic import op

revision = "0006_service_binding_scope"
down_revision = "0005_service_bindings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("service_binding_revisions", sa.Column("project_id", sa.String(128)))
    op.execute(
        "UPDATE service_binding_revisions r SET project_id=b.project_id "
        "FROM service_bindings b WHERE r.service_binding_id=b.service_binding_id"
    )
    op.alter_column("service_binding_revisions", "project_id", nullable=False)
    op.add_column("service_binding_revisions", sa.Column("tls_ca_ref", sa.String(128)))
    op.drop_constraint(
        "service_binding_revisions_service_binding_id_fkey",
        "service_binding_revisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "service_binding_revisions_pkey", "service_binding_revisions", type_="primary"
    )
    op.drop_constraint("service_bindings_pkey", "service_bindings", type_="primary")
    op.create_primary_key(
        "service_bindings_pkey", "service_bindings", ["project_id", "service_binding_id"]
    )
    op.create_primary_key(
        "service_binding_revisions_pkey",
        "service_binding_revisions",
        ["project_id", "service_binding_id", "revision"],
    )
    op.create_foreign_key(
        "service_binding_revisions_binding_fkey",
        "service_binding_revisions",
        "service_bindings",
        ["project_id", "service_binding_id"],
        ["project_id", "service_binding_id"],
    )


def downgrade():
    op.drop_constraint(
        "service_binding_revisions_binding_fkey", "service_binding_revisions", type_="foreignkey"
    )
    op.drop_constraint(
        "service_binding_revisions_pkey", "service_binding_revisions", type_="primary"
    )
    op.drop_constraint("service_bindings_pkey", "service_bindings", type_="primary")
    op.create_primary_key("service_bindings_pkey", "service_bindings", ["service_binding_id"])
    op.create_primary_key(
        "service_binding_revisions_pkey",
        "service_binding_revisions",
        ["service_binding_id", "revision"],
    )
    op.create_foreign_key(
        "service_binding_revisions_service_binding_id_fkey",
        "service_binding_revisions",
        "service_bindings",
        ["service_binding_id"],
        ["service_binding_id"],
    )
    op.drop_column("service_binding_revisions", "tls_ca_ref")
    op.drop_column("service_binding_revisions", "project_id")
