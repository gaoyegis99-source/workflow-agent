#!/usr/bin/env python3
"""Validate a spatial workflow release and static map through an isolated Gateway."""

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

from playwright.sync_api import expect, sync_playwright


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
                    value = payload
                return response.status, value, response.headers
        except urllib.error.HTTPError as error:
            payload = error.read()
            try:
                value = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = payload
            return error.code, value, error.headers

    def must(self, method: str, path: str, *, body=None, raw=None, key=None, statuses=(200,)):
        status, value, headers = self.call(method, path, body=body, raw=raw, key=key)
        if status not in statuses:
            error_code = value.get("code") if isinstance(value, dict) else None
            message = value.get("message") if isinstance(value, dict) else None
            raise RuntimeError(
                f"{method} {path} returned HTTP {status} "
                f"({error_code or 'no_error_code'}): {message or 'no_message'}"
            )
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


def wait_for(gateway: Gateway, method: str, path: str, terminal: set[str], timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value, _ = gateway.must(method, path)
        state = value.get("status") or value.get("state") or value.get("case_verdict")
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
        "evidence_kind": "spatial_workflow_release_and_static_map_via_gateway",
        "production_business_data_read": False,
        "status": "FAIL",
    }
    token_id = None
    try:
        principal = admin(env_file, "principal-create", "--name", args.run_id[:200])
        issued = admin(
            env_file,
            "token-issue",
            "--principal-id",
            principal["principal_id"],
            "--ttl-seconds",
            "3600",
        )
        token, token_id = issued["token"], issued["token_id"]
        gateway = Gateway(args.origin, token)
        project, project_headers = gateway.must(
            "POST",
            "/api/v1/projects",
            body={"name": args.run_id + " synthetic spatial workflow"},
            key=args.run_id + "-project",
            statuses=(201,),
        )
        project_id = project["project_id"]
        base = f"/api/v1/projects/{project_id}"
        result["project_id"] = project_id
        result["release_digest"] = project_headers.get("X-V7-Release-Digest")

        content = Path("tests/p42/fixtures/points-utm.gpkg").read_bytes()
        upload, _ = gateway.must(
            "POST",
            base + "/uploads",
            body={
                "filename": "synthetic-points-utm.gpkg",
                "format": "geopackage",
                "size": len(content),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            },
            key=args.run_id + "-upload",
            statuses=(201,),
        )
        upload_id = upload["upload_id"]
        gateway.must("PUT", base + f"/uploads/{upload_id}/content", raw=content)
        gateway.must(
            "POST",
            base + f"/uploads/{upload_id}/finalize",
            body={"kind": "SPATIAL", "profile": "strict-spatial-v1"},
            key=args.run_id + "-finalize",
            statuses=(202,),
        )
        uploaded = wait_for(
            gateway, "GET", base + f"/uploads/{upload_id}", {"READY", "FAILED"}, timeout=90
        )
        if uploaded["status"] != "READY" or not uploaded.get("asset"):
            raise RuntimeError("Synthetic GeoPackage did not become a spatial Asset")
        asset_ref = uploaded["asset"]
        asset, _ = gateway.must(
            "GET",
            base + f"/assets/{asset_ref['asset_id']}/versions/{asset_ref['version_id']}",
        )
        representation = next(
            item
            for item in asset["manifest"]["representations"]
            if item["representation_id"] == "original"
        )
        selection = {
            "asset": asset_ref,
            "representation_id": "original",
            "representation_digest": representation["digest"],
            "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
            "semantic_revision": None,
        }

        source = json.loads(Path("examples/workflows/qgis-buffer-summary-map-v1.json").read_text())
        root = source["documents"]["main"]
        root["inputs"] = {"vector": "File"}
        root["steps"]["buffer"]["in"]["parameters"] = {
            "default": {
                "distance": 100,
                "unit": "metre",
                "output_crs": "EPSG:32650",
                "segments": 8,
            }
        }
        root["steps"]["summary"]["in"]["parameters"] = {"default": {"layer_id": "l0"}}
        root["steps"]["map"]["in"]["parameters"] = {
            "default": {
                "layer_id": "l0",
                "template_id": "v7-spatial-basic-v1",
                "output_crs": "EPSG:32650",
                "title": "G1 synthetic buffer map",
                "legend": True,
                "palette": "blue",
            }
        }
        definition, _ = gateway.must(
            "POST",
            base + "/workflow-definitions",
            body={
                "expected_revision": 0,
                "stable_key": "g1-spatial-workflow",
                "metadata": {"title": "G1 synthetic spatial workflow"},
                "layout": {},
            },
            key=args.run_id + "-definition",
            statuses=(201,),
        )
        definition_id = definition["definition_id"]
        definition_path = base + f"/workflow-definitions/{definition_id}"
        revision, _ = gateway.must(
            "POST",
            definition_path + "/revisions",
            body={
                "expected_revision": 1,
                "source_digest": definition["head"]["source_digest"],
                "source": source,
                "metadata": {"title": "G1 synthetic spatial workflow"},
                "layout": {},
            },
            key=args.run_id + "-revision",
            statuses=(201,),
        )
        descriptor, _ = gateway.must("GET", base + "/authoring/operator-descriptors")
        validation, _ = gateway.must(
            "POST",
            definition_path + "/validations",
            body={
                "revision": revision["revision"],
                "source_digest": revision["source_digest"],
                "semantic_contract_digest": descriptor["semantic_contract_digest"],
            },
            key=args.run_id + "-validation",
            statuses=(202,),
        )
        validation_view = wait_for(
            gateway,
            "GET",
            validation["status_url"],
            {"PASSED", "REJECTED", "ERROR"},
        )
        result["validation_status"] = validation_view["status"]
        if validation_view["status"] != "PASSED":
            raise RuntimeError("Spatial workflow validation did not pass")

        candidate, _ = gateway.must(
            "POST",
            definition_path + "/candidates",
            body={
                "revision": revision["revision"],
                "source_digest": revision["source_digest"],
                "validation_id": validation["validation_id"],
            },
            key=args.run_id + "-candidate",
            statuses=(202,),
        )
        candidate_view = wait_for(
            gateway,
            "GET",
            candidate["status_url"],
            {"READY", "REJECTED"},
        )
        result["candidate_status"] = candidate_view["status"]
        if candidate_view["status"] != "READY" or not candidate_view.get("package_ref"):
            raise RuntimeError("Spatial workflow candidate did not become READY")

        catalog = json.loads((release_dir / "operator-catalog.json").read_text())
        operators = {item["operator_id"]: item for item in catalog["operators"]}
        operator_ids = ["qgis-buffer", "spatial-vector-summary", "qgis-static-map"]
        targets = []
        for operator_id in operator_ids:
            operator = operators[operator_id]
            targets.append(
                {
                    "kind": "OPERATOR",
                    "operator_id": operator_id,
                    "version_id": operator["version_id"],
                    "execution_binding": {
                        "kind": "LOCAL",
                        "implementation_id": operator["implementations"][0]["implementation_id"],
                    },
                }
            )
        workflow_target = {"kind": "WORKFLOW", "package_ref": candidate_view["package_ref"]}
        task, _ = gateway.must(
            "POST",
            base + "/tasks",
            body={
                "execution_mode": "DIRECT",
                "spec": {
                    "goal": "Qualify and execute a synthetic spatial workflow",
                    "requirements": [],
                },
                "budget": {"execution_count": 10, "max_in_flight": 2},
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
                "targets": [workflow_target, *targets],
                "resources": [asset_ref],
                "derived_outputs": True,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
            key=args.run_id + "-grant",
            statuses=(201,),
        )

        trial_inputs = {"vector": selection}
        trial, _ = gateway.must(
            "POST",
            definition_path + "/trial-runs",
            body={
                "candidate_id": candidate_view["candidate_id"],
                "task_id": task_id,
                "expected_spec_revision": 1,
                "inputs": trial_inputs,
                "max_node_executions": 3,
                "trial_expectations": [],
            },
            key=args.run_id + "-trial",
            statuses=(202,),
        )
        trial_view = wait_for(
            gateway,
            "GET",
            definition_path + f"/trial-runs/{trial['trial_id']}",
            {"PASS", "FAIL", "ERROR"},
            timeout=360,
        )
        result["trial_verdict"] = trial_view["case_verdict"]
        if trial_view["case_verdict"] != "PASS":
            raise RuntimeError("Real spatial workflow trial did not pass")

        release, _ = gateway.must(
            "POST",
            definition_path + "/releases",
            body={
                "candidate_id": candidate_view["candidate_id"],
                "version_label": "1.0.0",
                "trial_ids": [trial["trial_id"]],
            },
            key=args.run_id + "-release",
            statuses=(201,),
        )
        lifecycle_path = definition_path + "/releases/1.0.0/lifecycle-revisions"
        disabled, _ = gateway.must(
            "POST",
            lifecycle_path,
            body={
                "expected_lifecycle_revision": 1,
                "lifecycle": "DISABLED",
                "reason": "G1 lifecycle transition smoke evidence",
            },
            key=args.run_id + "-disable",
            statuses=(201,),
        )
        active, _ = gateway.must(
            "POST",
            lifecycle_path,
            body={
                "expected_lifecycle_revision": disabled["lifecycle_revision"],
                "lifecycle": "ACTIVE",
                "reason": "Restore the qualified release after lifecycle verification",
            },
            key=args.run_id + "-reactivate",
            statuses=(201,),
        )
        result["workflow_release"] = {
            "version_label": release["version_label"],
            "qualification": release["qualification"],
            "disabled_revision": disabled["lifecycle_revision"],
            "active_revision": active["lifecycle_revision"],
        }

        command, _ = gateway.must(
            "POST",
            definition_path + "/releases/1.0.0/executions",
            body={
                "task_id": task_id,
                "expected_spec_revision": 1,
                "inputs": trial_inputs,
                "max_node_executions": 3,
            },
            key=args.run_id + "-execute-release",
            statuses=(202,),
        )
        command_view = wait_for(
            gateway,
            "GET",
            command["status_url"],
            {"RESOLVED", "REJECTED"},
            timeout=360,
        )
        if command_view["status"] != "RESOLVED" or not command_view.get("result"):
            raise RuntimeError("Released workflow execution command did not resolve")
        execution_id = command_view["result"]["execution_id"]
        execution = wait_for(
            gateway,
            "GET",
            base + f"/executions/{execution_id}",
            {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"},
            timeout=360,
        )
        result["release_execution_state"] = execution["state"]
        if execution["state"] != "SUCCEEDED":
            raise RuntimeError("Released spatial workflow execution did not succeed")
        steps = gateway.must("GET", base + f"/executions/{execution_id}/steps")[0]["items"]
        if len(steps) != 3 or any(step["state"] != "SUCCEEDED" for step in steps):
            raise RuntimeError("Released workflow did not publish all three successful steps")

        assets_page, _ = gateway.must("GET", base + "/assets?limit=100")
        map_assets = [
            item
            for item in assets_page["items"]
            if item.get("manifest", {}).get("result_kind") == "CARTOGRAPHIC_PRODUCT"
            or item.get("manifest", {}).get("kind") == "CARTOGRAPHIC_PRODUCT"
        ]
        png = None
        map_ref = None
        for item in map_assets:
            map_ref = item["ref"]
            map_view, _ = gateway.must(
                "GET",
                base + f"/assets/{map_ref['asset_id']}/versions/{map_ref['version_id']}",
            )
            image = next(
                (
                    rep
                    for rep in map_view["manifest"]["representations"]
                    if rep["representation_id"] == "image"
                ),
                None,
            )
            if image:
                status, payload, headers = gateway.call(
                    "GET",
                    base
                    + f"/assets/{map_ref['asset_id']}/versions/{map_ref['version_id']}"
                    + f"/representations/{image['representation_id']}/content",
                )
                png_signature = b"\x89PNG\r\n\x1a\n"
                if (
                    status == 200
                    and isinstance(payload, bytes)
                    and payload.startswith(png_signature)
                ):
                    png = {
                        "status": status,
                        "byte_count": len(payload),
                        "digest_header": headers.get("Digest"),
                    }
                    break
        if png is None or map_ref is None:
            raise RuntimeError("Published static map PNG was not downloadable")
        result["static_map_download"] = png

        summary, _ = gateway.must("GET", base + "/workspace-summary")
        if summary["resources"]["assets"] < 4:
            raise RuntimeError("Workspace summary omitted newly published assets")
        browser_errors = []
        browser_api_errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda _error: browser_errors.append("pageerror"))
            page.on(
                "response",
                lambda response: (
                    browser_api_errors.append(response.status)
                    if "/api/v1/" in response.url and response.status >= 400
                    else None
                ),
            )
            page.goto(args.origin.rstrip("/") + "/")
            page.get_by_label("临时 Bearer Token").fill(token)
            page.get_by_role("button", name="连接并加载项目").click()
            page.get_by_role("button", name="进入工作区").click()
            page.get_by_role("link", name="数据资产").click()
            page.get_by_placeholder("按资产 ID 筛选").fill(map_ref["asset_id"])
            page.get_by_text(map_ref["asset_id"], exact=True).click()
            page.get_by_role("tab", name="静态地图").click()
            page.get_by_role("button", name="加载预览").click()
            expect(page.get_by_role("img", name=f"{map_ref['asset_id']} 静态地图")).to_be_visible(
                timeout=20_000
            )
            page.reload()
            expect(page.get_by_label("临时 Bearer Token")).to_be_visible()
            if page.get_by_label("临时 Bearer Token").input_value():
                raise RuntimeError("Bearer token persisted across browser refresh")
            browser.close()
        if browser_errors or browser_api_errors:
            raise RuntimeError("Static map browser preview emitted an error")
        budget, _ = gateway.must("GET", base + f"/tasks/{task_id}/budget")
        if budget["in_flight"] or budget["reserved"]:
            raise RuntimeError("Spatial trial and release execution budget did not settle")
        result.update(
            {
                "status": "PASS",
                "definition_id": definition_id,
                "validation_id": validation["validation_id"],
                "candidate_id": candidate_view["candidate_id"],
                "trial_id": trial["trial_id"],
                "release_id": release["release_id"],
                "execution_id": execution_id,
                "successful_step_count": len(steps),
                "static_map_asset_id": map_ref["asset_id"],
                "browser_static_map_preview": True,
                "browser_page_errors": len(browser_errors),
                "browser_api_errors": browser_api_errors,
                "final_budget": {
                    "used": budget["used"],
                    "reserved": budget["reserved"],
                    "in_flight": budget["in_flight"],
                },
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
                "trial_verdict": result.get("trial_verdict"),
                "release_execution_state": result.get("release_execution_state"),
                "static_map_download": result.get("static_map_download"),
                "output": str(args.output),
            }
        )
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
