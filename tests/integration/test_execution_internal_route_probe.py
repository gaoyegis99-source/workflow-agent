"""Direct HTTP evidence for the execution workflow bridge route inventory."""

from fastapi.testclient import TestClient
from platform_execution.api import create_app

from tests.platform.database.conftest import postgres  # noqa: F401
from tests.platform.services.conftest import service_databases  # noqa: F401


def test_execution_workflow_bridge_routes_have_observed_rejections(service_databases):  # noqa: F811
    execution_database = service_databases[1]
    with TestClient(create_app(execution_database, "platform-" + "x" * 32)) as client:
        routes = (
            ("POST", "/internal/workflows/missing/nodes/missing", {}),
            ("GET", "/internal/workflows/missing/nodes/missing?after_revision=0", None),
            ("POST", "/internal/workflows/missing/nodes/missing/trace", {}),
            ("GET", "/internal/workflows/missing/nodes/missing/files/result", None),
        )
        observed = []
        for method, path, body in routes:
            response = (
                client.request(method, path, json=body) if body is not None else client.get(path)
            )
            assert response.status_code == 404, response.text
            observed.append(response.status_code)
        assert observed == [404, 404, 404, 404]
