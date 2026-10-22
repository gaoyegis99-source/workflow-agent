"""P3 scoped validation grants, delivery and completion; retain all P2 history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_p3_delivery"
down_revision = "0002_observation_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "grants",
        sa.Column("derived_outputs", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "grants", sa.Column("acceptance_profiles", JSONB, nullable=False, server_default="[]")
    )
    op.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_lifecycle_check")
    op.create_check_constraint(
        "tasks_lifecycle_check", "tasks", "lifecycle IN ('OPEN','CANCELLED','COMPLETED')"
    )
    from platform_infrastructure.migrations.schema_0003 import metadata

    metadata.create_all(op.get_bind())


def downgrade():
    for name in ["completions", "acceptances", "deliverables", "execution_permits"]:
        if op.get_bind().scalar(sa.text("SELECT count(*) FROM " + name)):
            raise RuntimeError("Cannot downgrade populated P3 delivery state")
        op.drop_table(name)
    op.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_lifecycle_check")
    op.create_check_constraint(
        "tasks_lifecycle_check", "tasks", "lifecycle IN ('OPEN','CANCELLED')"
    )
    op.drop_column("grants", "acceptance_profiles")
    op.drop_column("grants", "derived_outputs")
