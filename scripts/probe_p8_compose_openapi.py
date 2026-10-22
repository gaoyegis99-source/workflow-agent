#!/usr/bin/env python3
"""Probe every public OpenAPI operation through an isolated P8 Gateway.

The probe creates and revokes a short-lived principal token through the local
Compose administration CLI. It records route status and response headers,
never the token or response body. It checks authenticated route reachability;
resource-specific positive flows belong in their dedicated probes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
UUID = "00000000-0000-4000-8000-000000000000"
REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def request(origin: str, method: str, path: str, *, token: str | None = None,
            body: object | None = None, headers: dict[str, str] | None = None):
    request_headers = {"X-Request-ID": hashlib.md5((method + path).encode()).hexdigest()}
    if token:
        request_headers["Authorization"] = "Bearer " + token
    if headers:
        request_headers.update(headers)
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    http_request = urllib.request.Request(
        origin.rstrip("/") + path, data=data, headers=request_headers, method=method
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(http_request, timeout=15) as response:
            payload = response.read()
            try:
                value = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            return response.status, value, response.headers
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        return error.code, value, error.headers


def admin(compose: list[str], *args: str) -> dict:
    completed = subprocess.run(
        [*compose, "exec", "-T", "platform-api", "platform-admin", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def path_value(name: str, schema: dict, project_id: str) -> str:
    if name == "project_id":
        return project_id
    if schema.get("type") == "integer":
        return "1"
    if schema.get("format") == "uuid":
        return UUID
    if name.endswith("_id"):
        return "probe-" + name.removesuffix("_id")
    if name == "version_label":
        return "v1"
    return "probe"


def build_path(template: str, operation: dict, project_id: str) -> str:
    parameters = {
        item.get("name"): item
        for item in operation.get("parameters", [])
        if item.get("in") == "path"
    }
    path = re.sub(
        r"\{([^}]+)\}",
        lambda match: path_value(
            match.group(1),
            parameters.get(match.group(1), {}).get("schema", {}),
            project_id,
        ),
        template,
    )
    query = {}
    for item in operation.get("parameters", []):
        if item.get("in") != "query" or not item.get("required"):
            continue
        schema = item.get("schema", {})
        name = item["name"]
        if name == "limit":
            query[name] = "100"
        elif "enum" in schema:
            query[name] = schema["enum"][0]
        elif schema.get("type") == "integer":
            query[name] = str(schema.get("default", schema.get("minimum", 1)))
        else:
            query[name] = "probe"
    return path + ("?" + urllib.parse.urlencode(query) if query else "")


def body_for(template: str, method: str, index: int):
    if method.lower() not in {"post", "put", "patch"}:
        return None
    if template == "/api/v1/projects" and method.lower() == "post":
        return {"name": f"OpenAPI probe {index}"}
    if template.endswith("/uploads") and method.lower() == "post":
        return {
            "filename": f"openapi-{index}.md",
            "format": "markdown",
            "size": 1,
            "digest": "sha256:" + "0" * 64,
        }
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env_file = args.env_file.resolve()
    compose = [
        "docker", "compose", "--env-file", str(env_file), "-f", "deploy/p8.compose.yaml"
    ]
    created = admin(compose, "principal-create", "--name", args.run_id[:200])
    principal_id = created["principal_id"]
    issued = admin(
        compose, "token-issue", "--principal-id", principal_id, "--ttl-seconds", "900"
    )
    token = issued["token"]
    token_id = issued["token_id"]
    results = []
    failure = None
    try:
        project_status, project, project_headers = request(
            args.origin,
            "POST",
            "/api/v1/projects",
            token=token,
            body={"name": f"{args.run_id} synthetic project"},
            headers={"Idempotency-Key": args.run_id + "-project"},
        )
        if project_status != 201 or not isinstance(project, dict):
            raise RuntimeError(f"Unable to create probe project; HTTP {project_status}")
        project_id = project["project_id"]
        status, schema, _ = request(args.origin, "GET", "/api/v1/openapi.json")
        if status != 200 or not isinstance(schema, dict):
            raise RuntimeError(f"Unable to read Gateway OpenAPI schema; HTTP {status}")

        index = 0
        for template, path_item in sorted(schema["paths"].items()):
            for method, operation in sorted(path_item.items()):
                if method.lower() not in METHODS:
                    continue
                index += 1
                path = build_path(template, operation, project_id)
                request_headers = {"Idempotency-Key": f"{args.run_id}-{index}"}
                status, _payload, response_headers = request(
                    args.origin,
                    method.upper(),
                    path,
                    token=token,
                    body=body_for(template, method, index),
                    headers=request_headers,
                )
                request_id = response_headers.get("X-Request-ID", "")
                result = {
                    "method": method.upper(),
                    "path": template,
                    "operation_id": operation.get("operationId"),
                    "status_code": status,
                    "authenticated": status not in {401, 403},
                    "request_id_valid": bool(REQUEST_ID.fullmatch(request_id)),
                    "cache_control": response_headers.get("Cache-Control"),
                    "release_digest": response_headers.get("X-V7-Release-Digest"),
                }
                results.append(result)

        invalid = [
            item
            for item in results
            if not item["authenticated"]
            or item["status_code"] in {405, 500, 503}
            or not item["request_id_valid"]
            or item["cache_control"] != "no-store"
        ]
        counts: dict[str, int] = {}
        for item in results:
            key = str(item["status_code"])
            counts[key] = counts.get(key, 0) + 1
        value = {
            "schema_version": 1,
            "run_id": args.run_id,
            "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "evidence_kind": "authenticated_route_response_via_isolated_gateway",
            "release_digest": project_headers.get("X-V7-Release-Digest"),
            "project_id": project_id,
            "operation_count": len(results),
            "authenticated_count": sum(item["authenticated"] for item in results),
            "invalid_count": len(invalid),
            "status_codes": counts,
            "invalid_operations": invalid,
            "production_business_data_read": False,
            "results": results,
        }
    except Exception as error:
        failure = error
        value = {
            "schema_version": 1,
            "run_id": args.run_id,
            "status": "ERROR",
            "error_type": type(error).__name__,
            "production_business_data_read": False,
        }
    finally:
        admin(compose, "token-revoke", "--token-id", token_id)

    value.setdefault(
        "status",
        "PASS"
        if value["operation_count"] == 90 and value["invalid_count"] == 0
        else "FAIL",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": value["status"],
                "operation_count": value.get("operation_count"),
                "authenticated_count": value.get("authenticated_count"),
                "invalid_count": value.get("invalid_count"),
                "status_codes": value.get("status_codes"),
                "output": str(args.output),
            }
        )
    )
    return 1 if failure or value["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
