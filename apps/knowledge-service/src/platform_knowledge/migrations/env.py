from alembic import context

from platform_knowledge.tables import metadata

connection = context.config.attributes.get("connection")
if connection is not None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()
