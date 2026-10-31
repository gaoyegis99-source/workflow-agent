import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import sqlalchemy as sa
from platform_contracts.control import GrantCreate
from platform_core.application.control import ControlService
from platform_infrastructure import tables
from platform_infrastructure.database import Database

from tests.pathing import SOURCE_ROOT

from .processes import Processes


@pytest.fixture(scope="session")
def postgres(request):
    # This fixture is re-exported by capability-local conftests.  Pytest can
    # register each re-export as a distinct session fixture, so cache the
    # disposable server on the one shared Session instead of starting dozens
    # of 512 MiB PostgreSQL containers during a full repository run.
    attribute = "_v7_shared_postgresql"
    environment = getattr(request.session, attribute, None)
    owner = environment is None
    if owner:
        path = SOURCE_ROOT / "scripts/configure/p2_environment.py"
        spec = importlib.util.spec_from_file_location("p2_environment", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        environment = module.PostgreSQL().__enter__()
        setattr(request.session, attribute, environment)
    try:
        yield environment
    finally:
        if owner:
            environment.__exit__(None, None, None)
            delattr(request.session, attribute)


@pytest.fixture
def database(postgres):
    database = Database(postgres.url)
    database.migrate()
    database.check_schema()
    with database.engine.begin() as connection:
        names = ", ".join('"' + table.name + '"' for table in tables.metadata.sorted_tables)
        connection.execute(sa.text("TRUNCATE " + names + " CASCADE"))
    yield database
    database.close()


@pytest.fixture
def owner(database):
    principal = database.create_principal("owner")
    token = database.issue_token(principal)
    return database.authenticate(token["token"]), token["token"]


TARGET = {
    "kind": "OPERATOR",
    "operator_id": "p2-fixture",
    "version_id": "1",
    "execution_binding": {"kind": "LOCAL", "implementation_id": "test-peer"},
}
TASK = {
    "execution_mode": "DIRECT",
    "spec": {"goal": "Check control semantics", "requirements": []},
    "budget": {"execution_count": 5, "max_in_flight": 2},
}


@pytest.fixture
def setup_task(database, owner, request):
    actor, token = owner
    app = ControlService(database)
    project = app.create_project(actor, "project", "P2")
    task = app.create_task(
        actor,
        project["project_id"],
        "task",
        TASK | {"budget": getattr(request, "param", TASK["budget"])},
    )
    grant = app.grant(
        actor,
        project["project_id"],
        "grant",
        GrantCreate(
            task_id=task["task_id"],
            targets=[TARGET],
            resources=[],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        ).model_dump(mode="json"),
    )
    return app, actor, token, project["project_id"], task["task_id"], grant


@pytest.fixture
def processes(tmp_path, postgres):
    # Fault timing tests require the peer's normal responses below five seconds.
    # This store survives peer SIGKILL/restart, not host power loss (outside P2).
    root = "/dev/shm" if Path("/dev/shm").is_dir() else None
    with TemporaryDirectory(prefix="v7-p2-peer-", dir=root) as state:
        harness = Processes(tmp_path, postgres.url, peer_directory=Path(state))
        try:
            yield harness
        finally:
            harness.close()
