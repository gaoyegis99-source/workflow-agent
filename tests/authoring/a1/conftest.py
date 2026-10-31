import sys

import httpx
import pytest

from tests.platform.database.conftest import database, owner, postgres  # noqa: F401
from tests.platform.database.processes import Processes, eventually
from tests.platform.services.harness import free_port


@pytest.fixture
def authoring_server(tmp_path, database):  # noqa: F811
    processes = Processes(tmp_path, database.engine.url.render_as_string(hide_password=False))
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    process = processes.start(
        [sys.executable, "-m", "platform_api", "--host", "127.0.0.1", "--port", str(port)],
        {"V7_DATABASE_URL": processes.database_url},
    )

    def ready():
        if process.poll() is not None:
            raise AssertionError("Authoring API exited during startup")
        try:
            return httpx.get(origin + "/api/v1/health", trust_env=False).status_code == 200
        except httpx.TransportError:
            return False

    eventually(ready, timeout=20)
    try:
        yield origin
    finally:
        processes.close()
