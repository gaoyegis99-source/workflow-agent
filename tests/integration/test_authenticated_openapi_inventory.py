"""Authenticated request evidence for every public OpenAPI operation.

The anonymous inventory probe is useful for checking the error envelope, but
it cannot prove that a bearer token reaches the route.  This probe creates one
isolated OWNER project, calls every documented operation with deterministic
placeholder inputs, and records the response after authentication.  A 4xx
validation/not-found response or a dependency 503 is valid here; 401/403,
405, and 500 indicate that the request did not exercise the authenticated
route contract.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_core.application.control import ControlService

from tests.p2.conftest import database, owner, postgres  # noqa: F401, F811

METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
UUID = "00000000-0000-4000-8000-000000000000"
REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def _value(name: str, parameter: dict, project_id: str) -> str:
    if name == "project_id":
        return project_id
    schema = parameter.get("schema", {})
    if schema.get("type") == "integer":
        return "1"
    if schema.get("format") == "uuid":
        return UUID
    if name.endswith("_id"):
        return UUID
    if name == "version_label":
        return "v1"
    return "probe"


def _path(template: str, operation: dict, project_id: str) -> str:
    parameters = {
        parameter.get("name"): parameter
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "path"
    }
    return re.sub(
        r"\{([^}]+)\}",
        lambda match: _value(match.group(1), parameters.get(match.group(1), {}), project_id),
        template,
    )


def _write_evidence(path: str | None, value: dict) -> None:
    if not path:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def test_authenticated_openapi_inventory(database, owner):  # noqa: F811
    actor, token = owner
    project = ControlService(database).create_project(actor, "authenticated-openapi", "Probe")
    project_id = project["project_id"]
    app = create_app(database)
    schema = app.openapi()
    results = []

    with TestClient(app, raise_server_exceptions=False) as client:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Actor-ID": actor.principal_id,
        }
        for template, path_item in sorted(schema["paths"].items()):
            for method, operation in sorted(path_item.items()):
                if method not in METHODS:
                    continue
                path = _path(template, operation, project_id)
                request_headers = {**headers, "X-Request-ID": f"{len(results):032x}"}
                if method in {"post", "patch", "put"}:
                    response = client.request(
                        method,
                        path,
                        headers=request_headers,
                        json={} if method != "put" else None,
                        content=b"{}" if method == "put" else None,
                    )
                else:
                    response = client.request(method, path, headers=request_headers)
                results.append(
                    {
                        "method": method.upper(),
                        "path": template,
                        "operation_id": operation.get("operationId"),
                        "status_code": response.status_code,
                        "authenticated": response.status_code not in {401, 403},
                        "request_id_valid": bool(
                            REQUEST_ID.fullmatch(response.headers.get("X-Request-ID", ""))
                        ),
                        "cache_control": response.headers.get("Cache-Control"),
                    }
                )

    invalid = [
        item
        for item in results
        if not item["authenticated"]
        or item["status_code"] in {405, 500}
        or not item["request_id_valid"]
        or item["cache_control"] != "no-store"
    ]
    value = {
        "schema_version": 1,
        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evidence_kind": "authenticated_route_response",
        "operation_count": len(results),
        "authenticated_count": sum(item["authenticated"] for item in results),
        "invalid_count": len(invalid),
        "results": results,
    }
    _write_evidence(os.environ.get("V7_AUTH_OPENAPI_EVIDENCE"), value)
    assert len(results) == 90
    assert not invalid, invalid
