"""Durable direct remote invocation ledger."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_remote_calls"
down_revision = "0004_execution_work_class"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "remote_calls",
        sa.Column(
            "action_id", sa.String(128), sa.ForeignKey("executions.action_id"), primary_key=True
        ),
        sa.Column("binding", JSONB, nullable=False),
        sa.Column("request_digest", sa.String(71), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("remote_execution_id", sa.String(128)),
        sa.Column("remote_revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("remote_state", sa.String(24)),
        sa.Column("executed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("observation", JSONB),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.CheckConstraint("remote_revision >= 0", name="remote_revision_nonnegative"),
    )


def downgrade():
    op.drop_table("remote_calls")
