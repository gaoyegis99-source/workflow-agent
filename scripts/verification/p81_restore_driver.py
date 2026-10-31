#!/usr/bin/env python3
"""Create and verify restored P8.1 business facts through the public HTTP gateway."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


class Driver:
    def __init__(self, origin: str, token: str):
        self.client = httpx.Client(
            base_url=origin,
            headers={"Authorization": "Bearer " + token},
            timeout=30,
            trust_env=False,
        )

    def request(self, method: str, path: str, body=None, *, key: str | None = None):
        headers = {"Idempotency-Key": key} if key else None
        response = self.client.request(method, path, json=body, headers=headers)
        if not response.is_success:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
        return response.json()

    def put(self, path: str, content: bytes):
        response = self.client.put(path, content=content)
        if not response.is_success:
            raise RuntimeError(f"PUT {path}: {response.status_code} {response.text}")
        return response.json()

    def wait(self, callback, *, timeout: float = 180):
        deadline = time.monotonic() + timeout
        while True:
            value = callback()
            if value is not None:
                return value
            if time.monotonic() >= deadline:
                raise TimeoutError("P8.1 restore business check did not converge")
            time.sleep(0.25)

    @staticmethod
    def target(operator: dict) -> dict:
        implementation = operator["implementations"][0]
        return {
            "kind": "OPERATOR",
            "operator_id": operator["operator_id"],
            "version_id": operator["version_id"],
            "execution_binding": {
                "kind": implementation["binding_kind"],
                "implementation_id": implementation["implementation_id"],
            },
        }

    def upload(self, prefix: str, filename: str, format_name: str, content: bytes, finalize: dict):
        created = self.request(
            "POST",
            prefix + "/uploads",
            {
                "filename": filename,
                "format": format_name,
                "size": len(content),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            },
            key="p81-upload-" + format_name,
        )
        path = prefix + "/uploads/" + created["upload_id"]
        self.put(path + "/content", content)
        self.request("POST", path + "/finalize", finalize, key="p81-finalize-" + format_name)

        def ready():
            result = self.request("GET", path)
            if result["status"] == "FAILED":
                raise RuntimeError(result)
            return result if result["status"] == "READY" else None

        return self.wait(ready, timeout=120)

    def execution(self, prefix: str, command: dict, *, timeout: float = 300):
        def finished():
            receipt = self.request("GET", command["status_url"])
            if not receipt.get("result"):
                if receipt["status"] == "REJECTED":
                    raise RuntimeError(receipt)
                return None
            result = self.request(
                "GET", prefix + "/executions/" + receipt["result"]["execution_id"]
            )
            return (
                result
                if result["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"}
                else None
            )

        return self.wait(finished, timeout=timeout)

    def seed(self) -> dict:
        project = self.request(
            "POST", "/api/v1/projects", {"name": "P8 clean restore evidence"}, key="p81-project"
        )
        project_id = project["project_id"]
        prefix = "/api/v1/projects/" + project_id
        operators = {
            item["operator_id"]: item
            for item in self.request("GET", prefix + "/operators")["items"]
        }
        p3_targets = [
            self.target(operators["timeseries-completeness"]),
            self.target(operators["statistics-linear-trend"]),
        ]
        csv = b"station,time,value\nA,2024-01-01,1\nA,2024-01-02,3\nA,2024-01-03,5\n"
        upload = self.upload(
            prefix,
            "p81-observations.csv",
            "csv",
            csv,
            {
                "columns": [
                    {"name": name, "type": "string"} for name in ("station", "time", "value")
                ]
            },
        )
        asset_ref = upload["asset"]
        version_id = asset_ref["version_id"]
        self.request(
            "POST",
            prefix + f"/assets/{asset_ref['asset_id']}/semantic-revisions",
            {
                "expected_revision": 0,
                "version_id": version_id,
                "semantics": {
                    "timezone": "UTC",
                    "value_unit": "m",
                    "quantity": "water_level_elevation",
                },
            },
            key="p81-semantics",
        )
        asset = self.request(
            "GET", prefix + f"/assets/{asset_ref['asset_id']}/versions/{version_id}"
        )
        normalized = next(
            item
            for item in asset["manifest"]["representations"]
            if item["representation_id"] == "normalized"
        )
        selection = {
            "asset": asset_ref,
            "representation_id": "normalized",
            "representation_digest": normalized["digest"],
            "semantic_revision": 1,
            "field_bindings": {"groupField": "f0", "timeField": "f1", "valueField": "f2"},
        }
        parameters = {
            "groups": ["A", "absent"],
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-05T00:00:00+00:00",
            "frequency": "day",
            "min_samples": 3,
        }
        acceptance_spec = {
            "profile": "p3-timeseries-v1",
            "input": selection,
            "parameters": parameters,
            "required_outputs": ["timeseries-completeness", "statistics-linear-trend"],
            "allow_not_evaluable": True,
            "minimum_evaluable": 1,
        }
        task = self.request(
            "POST",
            prefix + "/tasks",
            {
                "execution_mode": "DIRECT",
                "spec": {
                    "goal": "P8 restored P3 verified result",
                    "requirements": [],
                    "acceptance_spec": acceptance_spec,
                },
                "budget": {"execution_count": 3, "max_in_flight": 2},
            },
            key="p81-p3-task",
        )
        task_path = prefix + "/tasks/" + task["task_id"]
        try:
            self.request(
                "POST",
                prefix + "/grants",
                {
                    "task_id": task["task_id"],
                    "targets": p3_targets,
                    "resources": [asset_ref],
                    "derived_outputs": True,
                    "acceptance_profiles": ["p3-timeseries-v1"],
                    "expires_at": "2035-01-01T00:00:00+00:00",
                },
                key="p81-p3-grant",
            )
        except RuntimeError as error:
            # A driver restart created by an intentional API outage can encounter
            # the earlier time-relative grant. Its immutable grant remains valid.
            if "IDEMPOTENCY_CONFLICT" not in str(error):
                raise
        executions = []
        for index, target in enumerate(p3_targets):
            command = self.request(
                "POST",
                prefix + "/executions",
                {
                    "task_id": task["task_id"],
                    "expected_spec_revision": 1,
                    "target": target,
                    "inputs": {"observations": selection},
                    "parameters": parameters,
                },
                key=f"p81-p3-execution-{index}",
            )
            result = self.execution(prefix, command)
            if result["state"] != "SUCCEEDED":
                raise RuntimeError(result)
            executions.append(result)
        self.wait(
            lambda: (
                self.request("GET", task_path + "/budget")
                if self.request("GET", task_path + "/budget")["in_flight"] == 0
                else None
            )
        )
        deliverable = self.request(
            "POST",
            task_path + "/deliverables",
            {
                "expected_spec_revision": 1,
                "executions": [item["execution_id"] for item in executions],
            },
            key="p81-p3-deliverable",
        )
        acceptance = self.request(
            "POST",
            task_path + "/acceptance",
            {"expected_spec_revision": 1, "deliverable_id": deliverable["deliverable_id"]},
            key="p81-p3-acceptance",
        )
        report = self.wait(
            lambda: (
                value
                if (value := self.request("GET", acceptance["status_url"]))["status"] != "QUEUED"
                else None
            ),
            timeout=180,
        )
        if report["status"] != "PASS":
            raise RuntimeError(report)
        completed = self.request("GET", task_path)
        if completed["lifecycle"] != "COMPLETED":
            raise RuntimeError(completed)

        text = "地下水水位高程使用统一垂向基准。\n\nP8 冷备份必须保留精确证据引用。\n".encode()
        document_upload = self.upload(
            prefix,
            "p81-evidence.txt",
            "text",
            text,
            {"kind": "DOCUMENT", "profile": "utf8-text-v1"},
        )
        knowledge_import = self.request(
            "POST",
            prefix + "/knowledge/imports",
            {"source": document_upload["asset"], "representation_id": "original"},
            key="p81-knowledge-import",
        )

        def knowledge_ready():
            value = self.request(
                "GET", prefix + "/knowledge/imports/" + knowledge_import["import_id"]
            )
            if value["status"] == "FAILED":
                raise RuntimeError(value)
            return value if value["status"] == "READY" else None

        knowledge = self.wait(knowledge_ready, timeout=120)
        hits = self.request("POST", prefix + "/knowledge/search", {"query": "冷备份 精确证据"})
        if hits["status"] != "RESULTS":
            raise RuntimeError(hits)
        hit = hits["items"][0]
        unit_path = (
            prefix + f"/knowledge/documents/{hit['document_id']}/versions/1/units/{hit['unit_id']}"
        )
        evidence = self.request("GET", unit_path)
        return {
            "project_id": project_id,
            "asset_ref": asset_ref,
            "task_id": task["task_id"],
            "action_ids": [item["action_id"] for item in executions],
            "execution_ids": [item["execution_id"] for item in executions],
            "deliverable_id": deliverable["deliverable_id"],
            "acceptance_id": acceptance["acceptance_id"],
            "report_digest": report["report_digest"],
            "knowledge_import_id": knowledge_import["import_id"],
            "document_id": knowledge["document_id"],
            "unit_id": hit["unit_id"],
            "evidence_ref": evidence["evidence_ref"],
            "unit_path": unit_path,
        }

    def verify(self, state: dict) -> dict:
        prefix = "/api/v1/projects/" + state["project_id"]
        task_path = prefix + "/tasks/" + state["task_id"]
        task = self.request("GET", task_path)
        budget = self.request("GET", task_path + "/budget")
        acceptance = self.request("GET", task_path + "/acceptance/" + state["acceptance_id"])
        evidence = self.request("GET", state["unit_path"])
        executions = [
            self.request("GET", prefix + "/executions/" + execution_id)
            for execution_id in state["execution_ids"]
        ]
        if task["lifecycle"] != "COMPLETED" or acceptance["status"] != "PASS":
            raise RuntimeError("Restored P3 completion is not valid")
        if acceptance["report_digest"] != state["report_digest"]:
            raise RuntimeError("Acceptance report identity changed during restore")
        if evidence["evidence_ref"] != state["evidence_ref"]:
            raise RuntimeError("EvidenceRef changed during restore")
        if [item["action_id"] for item in executions] != state["action_ids"]:
            raise RuntimeError("Action identity changed during restore")
        if budget["used"] != 3 or budget["reserved"] != 0 or budget["in_flight"] != 0:
            raise RuntimeError("Budget facts changed during restore")
        withdrawn = self.request(
            "POST",
            prefix + f"/knowledge/documents/{state['document_id']}/versions/1/withdraw",
            {"expected_lifecycle_revision": 1, "reason": "P8 post-restore lifecycle test"},
            key="p81-restored-withdrawal",
        )
        historical = self.request("GET", state["unit_path"])
        if withdrawn["availability"] != "WITHDRAWN" or historical["availability"] != "WITHDRAWN":
            raise RuntimeError("Restored knowledge lifecycle did not advance")
        restored = {
            "project_id": state["project_id"],
            "task_id": state["task_id"],
            "action_ids": state["action_ids"],
            "execution_ids": state["execution_ids"],
            "acceptance_id": state["acceptance_id"],
            "evidence_ref_digest": state["evidence_ref"]["exact_text_digest"],
            "knowledge_availability": historical["availability"],
            "budget": {name: budget[name] for name in ("used", "reserved", "in_flight")},
        }
        if "spatial" in state:
            spatial = state["spatial"]
            execution = self.request("GET", prefix + "/executions/" + spatial["execution_id"])
            if execution["state"] != "SUCCEEDED" or execution["action_id"] != spatial["action_id"]:
                raise RuntimeError("Restored spatial workflow identity changed")
            receipt = execution["runtime"]["publication_receipt"]
            if receipt["state"] != "COMMITTED" or set(receipt["outputs"]) != set(
                spatial["outputs"]
            ):
                raise RuntimeError("Restored spatial publication changed")
            restored["spatial"] = {
                "action_id": execution["action_id"],
                "execution_id": execution["execution_id"],
                "publication": receipt["state"],
                "outputs": sorted(receipt["outputs"]),
            }
        return restored

    def seed_spatial(self, state: dict) -> dict:
        prefix = "/api/v1/projects/" + state["project_id"]
        operators = {
            item["operator_id"]: item
            for item in self.request("GET", prefix + "/operators")["items"]
        }
        content = (ROOT / "tests/spatial/native/fixtures/points-utm.gpkg").read_bytes()
        uploaded = self.upload(
            prefix,
            "p81-points-utm.gpkg",
            "geopackage",
            content,
            {"kind": "SPATIAL", "profile": "strict-spatial-v1"},
        )
        asset_ref = uploaded["asset"]
        asset = self.request(
            "GET", prefix + f"/assets/{asset_ref['asset_id']}/versions/{asset_ref['version_id']}"
        )
        original = next(
            item
            for item in asset["manifest"]["representations"]
            if item["representation_id"] == "original"
        )
        selection = {
            "asset": asset_ref,
            "representation_id": "original",
            "representation_digest": original["digest"],
            "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
            "semantic_revision": None,
        }
        source = json.loads(
            (ROOT / "examples/workflows/qgis-buffer-summary-map-v1.json").read_text(
                encoding="utf-8"
            )
        )
        queued = self.request(
            "POST", prefix + "/workflow-packages", source, key="p81-spatial-package-v2"
        )

        def package_ready():
            value = self.request("GET", queued["status_url"])
            if value["status"] == "REJECTED":
                raise RuntimeError(value)
            return value if value["status"] == "READY" else None

        package = self.wait(package_ready, timeout=180)
        workflow_target = {"kind": "WORKFLOW", "package_ref": package["package_ref"]}
        leaves = [
            self.target(operators[name])
            for name in ("qgis-buffer", "spatial-vector-summary", "qgis-static-map")
        ]
        task = self.request(
            "POST",
            prefix + "/tasks",
            {
                "execution_mode": "DIRECT",
                "spec": {"goal": "P8 restored QGIS map workflow", "requirements": []},
                "budget": {"execution_count": 3, "max_in_flight": 2},
            },
            key="p81-spatial-task",
        )
        self.request(
            "POST",
            prefix + "/grants",
            {
                "task_id": task["task_id"],
                "targets": [workflow_target, *leaves],
                "resources": [asset_ref],
                "derived_outputs": True,
                "expires_at": "2035-01-01T00:00:00+00:00",
            },
            key="p81-spatial-grant",
        )
        command = self.request(
            "POST",
            prefix + "/executions",
            {
                "task_id": task["task_id"],
                "expected_spec_revision": 1,
                "target": workflow_target,
                "inputs": {
                    "vector": selection,
                    "buffer_parameters": {
                        "distance": 100,
                        "unit": "metre",
                        "output_crs": "EPSG:32650",
                        "segments": 8,
                    },
                    "summary_parameters": {"layer_id": "l0"},
                    "map_parameters": {
                        "layer_id": "l0",
                        "template_id": "v7-spatial-basic-v1",
                        "output_crs": "EPSG:32650",
                        "title": "P8 恢复制图",
                        "legend": True,
                        "palette": "blue",
                    },
                },
                "max_node_executions": 3,
            },
            key="p81-spatial-execution",
        )
        execution = self.execution(prefix, command, timeout=600)
        if execution["state"] != "SUCCEEDED":
            raise RuntimeError(execution)
        receipt = execution["runtime"]["publication_receipt"]
        if receipt["state"] != "COMMITTED" or len(receipt["outputs"]) != 8:
            raise RuntimeError(receipt)
        state["spatial"] = {
            "task_id": task["task_id"],
            "action_id": execution["action_id"],
            "execution_id": execution["execution_id"],
            "outputs": sorted(receipt["outputs"]),
        }
        return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed", "seed-spatial", "verify"])
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    credentials = json.loads(args.token_file.read_text(encoding="utf-8"))
    driver = Driver(args.origin, credentials["token"])
    if args.command == "seed":
        value = driver.seed()
        args.state.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args.state.chmod(0o600)
    elif args.command == "seed-spatial":
        value = driver.seed_spatial(json.loads(args.state.read_text(encoding="utf-8")))
        args.state.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        value = driver.verify(json.loads(args.state.read_text(encoding="utf-8")))
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
