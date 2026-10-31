import pytest
import sqlalchemy as sa
from platform_contracts.service import ServiceFault, authenticate


def test_independent_schema_and_roles(service_databases):
    data, execution = service_databases
    for db in service_databases:
        db.migrate()
        db.check_schema()
    with data.engine.connect() as c:
        assert "assets" in sa.inspect(c).get_table_names()
        assert "executions" not in sa.inspect(c).get_table_names()
        assert not c.scalar(sa.text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user"))
    with execution.engine.connect() as c:
        assert "executions" in sa.inspect(c).get_table_names()
        assert "assets" not in sa.inspect(c).get_table_names()
        assert not c.scalar(sa.text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user"))
    denied = sa.create_engine(data.engine.url.set(database=execution.engine.url.database))
    try:
        with pytest.raises(sa.exc.OperationalError), denied.connect():
            pass
    finally:
        denied.dispose()


def test_internal_service_authentication():
    credentials = {"platform": "a" * 48, "execution": "b" * 48}
    assert authenticate("Bearer " + "a" * 48, credentials, {"platform"}) == "platform"
    for value in (None, "Bearer short", "Bearer " + "b" * 48):
        with pytest.raises(ServiceFault):
            authenticate(value, credentials, {"platform"})
