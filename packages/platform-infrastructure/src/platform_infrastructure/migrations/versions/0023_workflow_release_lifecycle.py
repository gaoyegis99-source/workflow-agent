"""Add workflow release lifecycle history and publication arbitration.

Revision ID: 0023_workflow_release_lifecycle
Revises: 0022_operator_proposals
"""

import sqlalchemy as sa
from alembic import op

revision = "0023_workflow_release_lifecycle"
down_revision = "0022_operator_proposals"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_releases",
        sa.Column("lifecycle", sa.String(16), nullable=False, server_default="ACTIVE"),
    )
    op.add_column(
        "workflow_releases",
        sa.Column("lifecycle_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "workflow_releases",
        sa.Column(
            "lifecycle_reason", sa.String(512), nullable=False, server_default="INITIAL_RELEASE"
        ),
    )
    op.create_check_constraint(
        "ck_workflow_release_lifecycle",
        "workflow_releases",
        "lifecycle IN ('ACTIVE','DISABLED','REVOKED')",
    )
    op.create_check_constraint(
        "ck_workflow_release_lifecycle_revision",
        "workflow_releases",
        "lifecycle_revision > 0",
    )

    op.create_table(
        "workflow_release_lifecycle_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column(
            "release_id",
            sa.String(128),
            sa.ForeignKey("workflow_releases.release_id"),
            nullable=False,
        ),
        sa.Column("lifecycle_revision", sa.Integer(), nullable=False),
        sa.Column("previous_lifecycle", sa.String(16)),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column(
            "created_by", sa.String(128), sa.ForeignKey("principals.principal_id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle IN ('ACTIVE','DISABLED','REVOKED')",
            name="ck_workflow_release_event_lifecycle",
        ),
        sa.CheckConstraint(
            "previous_lifecycle IS NULL OR previous_lifecycle IN ('ACTIVE','DISABLED','REVOKED')",
            name="ck_workflow_release_event_previous",
        ),
        sa.CheckConstraint("lifecycle_revision > 0", name="ck_workflow_release_event_revision"),
        sa.UniqueConstraint(
            "release_id",
            "lifecycle_revision",
            name="uq_workflow_release_lifecycle_revision",
        ),
    )
    op.create_index(
        "ix_workflow_release_lifecycle_history",
        "workflow_release_lifecycle_events",
        ["release_id", "lifecycle_revision"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO workflow_release_lifecycle_events
              (event_id, release_id, lifecycle_revision, previous_lifecycle,
               lifecycle, reason, created_by, created_at)
            SELECT md5(release_id || '-initial'), release_id, 1, NULL,
                   'ACTIVE', 'INITIAL_RELEASE', created_by, created_at
            FROM workflow_releases
            """
        )
    )

    op.create_table(
        "workflow_release_publication_permits",
        sa.Column(
            "action_id", sa.String(128), sa.ForeignKey("actions.action_id"), primary_key=True
        ),
        sa.Column(
            "release_id",
            sa.String(128),
            sa.ForeignKey("workflow_releases.release_id"),
            nullable=False,
        ),
        sa.Column("lifecycle_revision", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('ALLOW','DENY')", name="ck_workflow_release_permit_decision"
        ),
    )
    op.create_index(
        "ix_workflow_release_permits_release",
        "workflow_release_publication_permits",
        ["release_id", "action_id"],
    )


def downgrade():
    op.drop_index(
        "ix_workflow_release_permits_release",
        table_name="workflow_release_publication_permits",
    )
    op.drop_table("workflow_release_publication_permits")
    op.drop_index(
        "ix_workflow_release_lifecycle_history",
        table_name="workflow_release_lifecycle_events",
    )
    op.drop_table("workflow_release_lifecycle_events")
    op.drop_constraint("ck_workflow_release_lifecycle_revision", "workflow_releases", type_="check")
    op.drop_constraint("ck_workflow_release_lifecycle", "workflow_releases", type_="check")
    op.drop_column("workflow_releases", "lifecycle_reason")
    op.drop_column("workflow_releases", "lifecycle_revision")
    op.drop_column("workflow_releases", "lifecycle")
