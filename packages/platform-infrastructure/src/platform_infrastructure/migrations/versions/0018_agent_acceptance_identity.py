"""Allow a controlled Agent request to own a checker reservation.

Revision ID: 0018_agent_acceptance_identity
Revises: 0017_agent_delivery_attempts
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_agent_acceptance_identity"
down_revision = "0017_agent_delivery_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "acceptance_check_envelopes",
        "token_id",
        existing_type=sa.String(128),
        nullable=True,
    )


def downgrade():
    op.alter_column(
        "acceptance_check_envelopes",
        "token_id",
        existing_type=sa.String(128),
        nullable=False,
    )
