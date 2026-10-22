import sqlalchemy as sa
from platform_infrastructure import database as database_module


def test_p64_schema_has_action_links_and_nullable_agent_token(database):
    assert database_module.SCHEMA_VERSION == "0023_workflow_release_lifecycle"
    with database.engine.connect() as connection:
        version = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        columns = {
            row["column_name"]: row
            for row in connection.execute(
                sa.text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='actions'"
                )
            ).mappings()
        }
        links = connection.scalar(
            sa.text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='agent_action_links'"
            )
        )
    assert version == "0023_workflow_release_lifecycle"
    assert columns["token_id"]["is_nullable"] == "YES"
    assert links == 1
