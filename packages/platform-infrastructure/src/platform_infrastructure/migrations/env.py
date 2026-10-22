"""Only explicit platform-admin migrate invokes Alembic."""

from alembic import context
from platform_infrastructure.tables import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
