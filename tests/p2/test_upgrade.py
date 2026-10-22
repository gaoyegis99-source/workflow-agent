import pytest
from platform_core.application.control import ControlService
from platform_infrastructure.database import Database

from .legacy_upgrade import fingerprint, migrate_to, recover_known, seed, verify


def test_upgrade_0001_preserves_all_rows_and_recovers_existing_identity(database):
    migrate_to(database, "base", downgrade=True)
    migrate_to(database, "0001_control")
    try:
        fixture = seed(database)
        with pytest.raises(RuntimeError, match="migration version"):
            database.check_schema()
        database.migrate()
        verify(database, fixture)
        database.migrate()
        assert fingerprint(database) == fixture["fingerprint"]
        recover_known(database, fixture)
        # Connections reopened after upgrade see the same identity and ledger.
        reopened = Database(database.engine.url.render_as_string(hide_password=False))
        try:
            reopened.check_schema()
            app = ControlService(reopened)
            actor = reopened.authenticate(fixture["token"])
            assert app.budget(actor, fixture["project"], fixture["task"])["used"] == 1
        finally:
            reopened.close()
    finally:
        database.migrate()
