"""Index document uploads for the dedicated worker.

Revision ID: 0004_document_assets
Revises: 0003_spatial_import_runtime
"""

from alembic import op

revision = "0004_document_assets"
down_revision = "0003_spatial_import_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX ix_uploads_project_format_state "
        "ON uploads (project_id, ((payload ->> 'format')), state)"
    )


def downgrade():
    op.drop_index("ix_uploads_project_format_state", table_name="uploads")
