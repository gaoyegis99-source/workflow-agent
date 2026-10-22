"""Trusted checker capabilities and composed acceptance runs.

Revision ID: 0010_checker_runs
Revises: 0009_output_identity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_checker_runs"
down_revision = "0009_output_identity"
branch_labels = None
depends_on = None
ID = sa.String(128)
TIME = sa.DateTime(timezone=True)


def upgrade():
    op.alter_column("acceptances", "action_id", existing_type=ID, nullable=True)
    op.add_column(
        "acceptance_coverage_items",
        sa.Column("coverage_status", sa.String(16), nullable=False, server_default="COVERED"),
    )
    for name, type_ in [
        ("capability_id", ID),
        ("capability_version", sa.String(32)),
        ("capability_digest", sa.String(71)),
        ("check_run_id", ID),
    ]:
        op.add_column("acceptance_coverage_items", sa.Column(name, type_))
    op.create_table(
        "acceptance_check_envelopes",
        sa.Column(
            "acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), primary_key=True
        ),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("released", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
        sa.Column("token_id", ID, sa.ForeignKey("tokens.token_id"), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("catalog_digest", sa.String(71), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_acceptance_check_envelope_amount"),
    )
    op.create_table(
        "acceptance_check_runs",
        sa.Column("run_id", ID, primary_key=True),
        sa.Column("acceptance_id", ID, sa.ForeignKey("acceptances.acceptance_id"), nullable=False),
        sa.Column("run_key", ID, nullable=False),
        sa.Column("capability_id", ID, nullable=False),
        sa.Column("capability_version", sa.String(32), nullable=False),
        sa.Column("capability_digest", sa.String(71), nullable=False),
        sa.Column("implementation_id", ID, nullable=False),
        sa.Column("image_digest", sa.String(71), nullable=False),
        sa.Column("input_contract_digest", sa.String(71), nullable=False),
        sa.Column("output_contract_digest", sa.String(71), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("item_ids", JSONB, nullable=False),
        sa.Column("input_roles", JSONB, nullable=False),
        sa.Column("claim_revision_ids", JSONB, nullable=False),
        sa.Column("depends_on", JSONB, nullable=False),
        sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "retry_of_run_id", ID, sa.ForeignKey("acceptance_check_runs.run_id"), nullable=True
        ),
        sa.Column("report", JSONB),
        sa.Column("report_digest", sa.String(64)),
        sa.UniqueConstraint("acceptance_id", "run_key", name="uq_acceptance_check_run_key"),
    )


def downgrade():
    op.drop_table("acceptance_check_runs")
    op.drop_table("acceptance_check_envelopes")
    for name in ["check_run_id", "capability_digest", "capability_version", "capability_id"]:
        op.drop_column("acceptance_coverage_items", name)
    op.drop_column("acceptance_coverage_items", "coverage_status")
    op.alter_column("acceptances", "action_id", existing_type=ID, nullable=False)
