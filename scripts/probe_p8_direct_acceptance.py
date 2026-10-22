#!/usr/bin/env python3
"""Run a Direct Task from Gateway upload through independent acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path


class Gateway:
    def __init__(self, origin: str, token: str):
        self.origin = origin.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, *, body=None, raw=None, key=None):
        headers = {"Authorization": "Bearer " + self.token}
        if key:
            headers["Idempotency-Key"] = key
        if body is not None:
            raw = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.origin + path, data=raw, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=30) as response:
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

    def must(self, method: str, path: str, *, body=None, raw=None, key=None, statuses=(200,)):
        status, value, headers = self.call(
            method, path, body=body, raw=raw, key=key
        )
        if status not in statuses:
            raise RuntimeError(f"{method} {path} returned HTTP {status}")
        return value, headers


def admin(env_file: Path, *args: str) -> dict:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            "deploy/p8.compose.yaml",
            "exec",
            "-T",
            "platform-api",
            "platform-admin",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def wait_for(gateway: Gateway, method: str, path: str, terminal: set[str], timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value, _ = gateway.must(method, path)
        state = value.get("status") or value.get("state")
        if state in terminal:
            return value
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {method} {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env_file = args.env_file.resolve()
    release_dir = args.release_dir.resolve()
    result = {
        "schema_version": 1,
        "run_id": args.run_id,
        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evidence_kind": "direct_task_real_gateway_and_compose_workers",
        "production_business_data_read": False,
        "status": "FAIL",
    }
    principal_id = token_id = None
    try:
        created = admin(env_file, "principal-create", "--name", args.run_id[:200])
        principal_id = created["principal_id"]
        issued = admin(
            env_file,
            "token-issue",
            "--principal-id",
            principal_id,
            "--ttl-seconds",
            "3600",
        )
        token, token_id = issued["token"], issued["token_id"]
        gateway = Gateway(args.origin, token)
        project, project_headers = gateway.must(
            "POST",
            "/api/v1/projects",
            body={"name": args.run_id + " synthetic Direct Task"},
            key=args.run_id + "-project",
            statuses=(201,),
        )
        project_id = project["project_id"]
        base = f"/api/v1/projects/{project_id}"
        result["project_id"] = project_id
        result["release_digest"] = project_headers.get("X-V7-Release-Digest")

        content = (
            b"station,time,value\n"
            b"A,2024-01-01T00:00:00Z,1\n"
            b"A,2024-01-02T00:00:00Z,3\n"
            b"A,2024-01-03T00:00:00Z,5\n"
        )
        upload, _ = gateway.must(
            "POST",
            base + "/uploads",
            body={
                "filename": "synthetic-timeseries.csv",
                "format": "csv",
                "size": len(content),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            },
            key=args.run_id + "-upload",
            statuses=(201,),
        )
        upload_id = upload["upload_id"]
        gateway.must(
            "PUT", base + f"/uploads/{upload_id}/content", raw=content
        )
        gateway.must(
            "POST",
            base + f"/uploads/{upload_id}/finalize",
            body={
                "columns": [
                    {"name": "station", "type": "string"},
                    {"name": "time", "type": "string"},
                    {"name": "value", "type": "float64"},
                ],
                "null_values": ["", "NA"],
            },
            key=args.run_id + "-finalize",
            statuses=(202,),
        )
        uploaded = wait_for(
            gateway, "GET", base + f"/uploads/{upload_id}", {"READY", "FAILED"}
        )
        if uploaded["status"] != "READY" or not uploaded.get("asset"):
            raise RuntimeError("Synthetic CSV upload did not become an Asset")
        asset_ref = uploaded["asset"]
        asset, _ = gateway.must(
            "GET",
            base + f"/assets/{asset_ref['asset_id']}/versions/{asset_ref['version_id']}",
        )
        semantic, _ = gateway.must(
            "POST",
            base + f"/assets/{asset_ref['asset_id']}/semantic-revisions",
            body={
                "expected_revision": 0,
                "version_id": asset_ref["version_id"],
                "semantics": {
                    "timezone": "UTC",
                    "value_unit": "m",
                    "quantity": "water_level_elevation",
                    "missing_values": ["", "NA"],
                },
            },
            key=args.run_id + "-semantics",
            statuses=(201,),
        )
        representation = next(
            item
            for item in asset["manifest"]["representations"]
            if item["representation_id"] == "normalized"
        )
        selection = {
            "asset": asset_ref,
            "representation_id": "normalized",
            "representation_digest": representation["digest"],
            "semantic_revision": semantic["revision"],
            "field_bindings": {
                "groupField": "f0",
                "timeField": "f1",
                "valueField": "f2",
            },
        }
        parameters = {
            "groups": ["A", "absent"],
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-05T00:00:00+00:00",
            "frequency": "day",
            "min_samples": 3,
        }

        catalog = json.loads((release_dir / "operator-catalog.json").read_text())
        operators = {item["operator_id"]: item for item in catalog["operators"]}
        selected = [
            operators[name]
            for name in ("timeseries-completeness", "statistics-linear-trend")
        ]
        targets = [
            {
                "kind": "OPERATOR",
                "operator_id": item["operator_id"],
                "version_id": item["version_id"],
                "execution_binding": {
                    "kind": "LOCAL",
                    "implementation_id": item["implementations"][0]["implementation_id"],
                },
            }
            for item in selected
        ]
        acceptance_spec = {
            "profile": "p3-timeseries-v1",
            "input": selection,
            "parameters": parameters,
            "required_outputs": [item["operator_id"] for item in selected],
            "allow_not_evaluable": True,
            "minimum_evaluable": 1,
        }
        task, _ = gateway.must(
            "POST",
            base + "/tasks",
            body={
                "execution_mode": "DIRECT",
                "spec": {
                    "goal": "Run and independently verify synthetic groundwater trend outputs",
                    "requirements": [],
                    "acceptance_spec": acceptance_spec,
                },
                "budget": {"execution_count": 3, "max_in_flight": 2},
            },
            key=args.run_id + "-task",
            statuses=(201,),
        )
        task_id = task["task_id"]
        result["task_id"] = task_id
        grant, _ = gateway.must(
            "POST",
            base + "/grants",
            body={
                "task_id": task_id,
                "targets": targets,
                "resources": [asset_ref],
                "derived_outputs": True,
                "acceptance_profiles": ["p3-timeseries-v1"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
            key=args.run_id + "-grant",
            statuses=(201,),
        )
        command_receipts = []
        execution_ids = []
        for index, target in enumerate(targets):
            command, _ = gateway.must(
                "POST",
                base + "/executions",
                body={
                    "task_id": task_id,
                    "expected_spec_revision": 1,
                    "target": target,
                    "inputs": {"observations": selection},
                    "parameters": parameters,
                },
                key=f"{args.run_id}-execution-{index}",
                statuses=(202,),
            )
            receipt = wait_for(
                gateway,
                "GET",
                command["status_url"],
                {"RESOLVED", "REJECTED"},
            )
            if receipt["status"] != "RESOLVED" or not receipt.get("result"):
                raise RuntimeError("Direct execution command was not resolved")
            execution_id = receipt["result"]["execution_id"]
            execution = wait_for(
                gateway,
                "GET",
                base + f"/executions/{execution_id}",
                {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"},
            )
            if execution["state"] != "SUCCEEDED":
                raise RuntimeError("Direct execution did not succeed")
            execution_ids.append(execution_id)
            command_receipts.append(
                {"status": receipt["status"], "execution_state": execution["state"]}
            )

        budget = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            budget, _ = gateway.must("GET", base + f"/tasks/{task_id}/budget")
            if budget["in_flight"] == 0 and budget["reserved"] == 0:
                break
            time.sleep(0.25)
        if budget is None:
            raise RuntimeError("Direct execution budget did not become readable")
        if budget["used"] != 2 or budget["in_flight"] != 0 or budget["reserved"] != 0:
            raise RuntimeError("Direct task budget did not settle at two used executions")
        deliverable, _ = gateway.must(
            "POST",
            base + f"/tasks/{task_id}/deliverables",
            body={"expected_spec_revision": 1, "executions": execution_ids},
            key=args.run_id + "-deliverable",
            statuses=(201,),
        )
        acceptance, _ = gateway.must(
            "POST",
            base + f"/tasks/{task_id}/acceptance",
            body={"expected_spec_revision": 1, "deliverable_id": deliverable["deliverable_id"]},
            key=args.run_id + "-acceptance",
            statuses=(202,),
        )
        report = wait_for(
            gateway,
            "GET",
            acceptance["status_url"],
            {"PASS", "FAIL", "ERROR", "NOT_EVALUABLE", "INDETERMINATE", "REVIEW_REQUIRED"},
        )
        if report["status"] != "PASS":
            raise RuntimeError("Direct Task independent acceptance did not pass")
        final_task = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            final_task, _ = gateway.must("GET", base + f"/tasks/{task_id}")
            if final_task["lifecycle"] == "COMPLETED":
                break
            time.sleep(0.25)
        final_budget, _ = gateway.must("GET", base + f"/tasks/{task_id}/budget")
        if (
            final_budget["used"] != 3
            or final_budget["in_flight"] != 0
            or final_budget["reserved"] != 0
        ):
            raise RuntimeError("Direct Task acceptance budget did not settle")

        assets_page, _ = gateway.must("GET", base + "/assets?limit=100")
        outputs = []
        for item in assets_page["items"]:
            manifest = item.get("manifest", {})
            if manifest.get("result_kind", manifest.get("kind")) != "OPERATOR_RESULT":
                continue
            ref = item["ref"]
            version, _ = gateway.must(
                "GET", base + f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
            )
            representation_id = version["manifest"]["representations"][0]["representation_id"]
            content_status, _body, _headers = gateway.call(
                "GET",
                base
                + f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
                + f"/representations/{representation_id}/content",
            )
            if content_status != 200:
                raise RuntimeError("Published Direct Task result download did not return HTTP 200")
            outputs.append(
                {
                    "asset_id": ref["asset_id"],
                    "version_id": ref["version_id"],
                    "download_status": content_status,
                }
            )
        if len(outputs) < 2:
            raise RuntimeError("Expected published Direct Task result assets were not found")
        if final_task is None or final_task["lifecycle"] != "COMPLETED":
            raise RuntimeError("Direct Task did not enter COMPLETED lifecycle")
        result.update(
            {
                "status": "PASS",
                "task_id": task_id,
                "grant_id": grant["grant_id"],
                "task_lifecycle": final_task["lifecycle"],
                "execution_count": len(execution_ids),
                "executions": command_receipts,
                "budget": {
                    "used": final_budget["used"],
                    "reserved": final_budget["reserved"],
                    "in_flight": final_budget["in_flight"],
                },
                "acceptance_status": report["status"],
                "result_downloads": outputs,
                "credential_revoked_in_finally": True,
            }
        )
    except Exception as error:
        result["error_type"] = type(error).__name__
        raise
    finally:
        if token_id:
            admin(env_file, "token-revoke", "--token-id", token_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "task_id": result.get("task_id"),
                "output": str(args.output),
            }
        )
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
