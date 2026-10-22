#!/usr/bin/env python3
"""Exercise 101-item upload, asset and Knowledge pagination through P8 Compose.

All requests use a short-lived token and one synthetic project. The browser
connects to the configured Gateway; the API and workers are the real Compose
services. Evidence excludes credentials and document text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def compose_command(env_file: Path, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        "deploy/p8.compose.yaml",
        *args,
    ]


def admin(env_file: Path, *args: str) -> dict:
    completed = subprocess.run(
        compose_command(env_file, "exec", "-T", "platform-api", "platform-admin", *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


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
                return response.status, value
        except urllib.error.HTTPError as error:
            payload = error.read()
            try:
                value = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            return error.code, value

    def must(self, method: str, path: str, *, body=None, raw=None, key=None, statuses=(200,)):
        status, value = self.call(method, path, body=body, raw=raw, key=key)
        if status not in statuses:
            raise RuntimeError(f"{method} {path} returned HTTP {status}")
        return value


def wait_for_upload(gateway: Gateway, project_id: str, upload_id: str) -> dict:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        value = gateway.must("GET", f"/api/v1/projects/{project_id}/uploads/{upload_id}")
        if value["status"] in {"READY", "FAILED"}:
            return value
        time.sleep(0.25)
    raise RuntimeError("Upload worker did not reach a terminal state")


def wait_for_import(gateway: Gateway, project_id: str, import_id: str) -> dict:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        value = gateway.must(
            "GET", f"/api/v1/projects/{project_id}/knowledge/imports/{import_id}"
        )
        if value["status"] in {"READY", "FAILED"}:
            return value
        time.sleep(0.25)
    raise RuntimeError("Knowledge worker did not reach a terminal state")


def check_catalog(gateway: Gateway, path: str, expected: int) -> dict:
    first = gateway.must("GET", f"{path}?limit=100")
    cursor = first.get("next_cursor")
    if len(first.get("items", [])) != 100 or not cursor:
        raise RuntimeError(f"{path} did not return a 100-item first page and cursor")
    second = gateway.must(
        "GET", f"{path}?limit=100&after={urllib.parse.quote(cursor, safe='')}"
    )
    def ids(items):
        return [
            item.get("upload_id")
            or item.get("ref", {}).get("asset_id")
            or item.get("document_id")
            for item in items
        ]
    first_ids, second_ids = ids(first["items"]), ids(second["items"])
    combined = first_ids + second_ids
    if len(combined) != expected or len(set(combined)) != expected:
        raise RuntimeError(f"{path} pagination returned duplicates or an incorrect count")
    return {
        "first_page_count": len(first_ids),
        "second_page_count": len(second_ids),
        "next_cursor_present": True,
        "stable_unique_ids": True,
        "second_page_first_id": second_ids[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objects", type=int, default=101)
    args = parser.parse_args()
    if args.objects != 101:
        raise SystemExit("This acceptance probe is frozen at exactly 101 objects")

    env_file = args.env_file.resolve()
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
    result = {
        "schema_version": 1,
        "run_id": args.run_id,
        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evidence_kind": "real_gateway_and_compose_service_pagination",
        "objects_per_catalogue": args.objects,
        "production_business_data_read": False,
        "status": "FAIL",
    }
    stage = "setup"
    try:
        gateway = Gateway(args.origin, token)
        project = gateway.must(
            "POST",
            "/api/v1/projects",
            body={"name": f"{args.run_id} synthetic project"},
            key=args.run_id + "-project",
            statuses=(201,),
        )
        project_id = project["project_id"]
        result["project_id"] = project_id

        stage = "seed_upload_asset"
        upload_ids = []
        for index in range(args.objects):
            content = (
                f"# Synthetic groundwater evidence {index:03d}\n"
                f"Monitoring well SYN-{index:03d} has synthetic value {index + 10}.\n"
            ).encode()
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            upload = gateway.must(
                "POST",
                f"/api/v1/projects/{project_id}/uploads",
                body={
                    "filename": f"synthetic-{index:03d}.md",
                    "format": "markdown",
                    "size": len(content),
                    "digest": digest,
                },
                key=f"{args.run_id}-upload-{index:03d}",
                statuses=(201,),
            )
            upload_id = upload["upload_id"]
            gateway.must(
                "PUT",
                f"/api/v1/projects/{project_id}/uploads/{upload_id}/content",
                raw=content,
                statuses=(200,),
            )
            gateway.must(
                "POST",
                f"/api/v1/projects/{project_id}/uploads/{upload_id}/finalize",
                body={"kind": "DOCUMENT", "profile": "utf8-text-v1"},
                key=f"{args.run_id}-finalize-{index:03d}",
                statuses=(202,),
            )
            upload_ids.append(upload_id)

        stage = "wait_upload_asset_workers"
        with ThreadPoolExecutor(max_workers=12) as pool:
            uploads = list(
                pool.map(
                    lambda item: wait_for_upload(gateway, project_id, item), upload_ids
                )
            )
        if any(item["status"] != "READY" or not item.get("asset") for item in uploads):
            raise RuntimeError("At least one synthetic upload failed asset generation")

        stage = "seed_knowledge"
        imports = []
        for index, upload in enumerate(uploads):
            receipt = gateway.must(
                "POST",
                f"/api/v1/projects/{project_id}/knowledge/imports",
                body={"source": upload["asset"], "representation_id": "original"},
                key=f"{args.run_id}-knowledge-{index:03d}",
                statuses=(202,),
            )
            imports.append(receipt["import_id"])
        with ThreadPoolExecutor(max_workers=12) as pool:
            documents = list(
                pool.map(
                    lambda item: wait_for_import(gateway, project_id, item), imports
                )
            )
        if any(item["status"] != "READY" or not item.get("document_id") for item in documents):
            raise RuntimeError("At least one Knowledge import failed")

        stage = "catalog_api_pagination"
        result["upload_pagination"] = check_catalog(
            gateway, f"/api/v1/projects/{project_id}/uploads", args.objects
        )
        result["asset_pagination"] = check_catalog(
            gateway, f"/api/v1/projects/{project_id}/assets", args.objects
        )
        result["knowledge_pagination"] = check_catalog(
            gateway, f"/api/v1/projects/{project_id}/knowledge/documents", args.objects
        )
        asset_page = gateway.must("GET", f"/api/v1/projects/{project_id}/assets?limit=100")
        asset_ref = asset_page["items"][0]["ref"]
        gateway.must(
            "GET",
            f"/api/v1/projects/{project_id}/assets/{asset_ref['asset_id']}"
            f"/versions/{asset_ref['version_id']}",
        )
        representations = asset_page["items"][0]["manifest"].get("representations", [])
        representation_id = representations[0]["representation_id"]
        content_status, _ = gateway.call(
            "GET",
            f"/api/v1/projects/{project_id}/assets/{asset_ref['asset_id']}"
            f"/versions/{asset_ref['version_id']}/representations/{representation_id}/content",
        )
        if content_status != 200:
            raise RuntimeError("Exact Asset representation download did not return HTTP 200")
        result["asset_exact_read"] = {
            "detail_status": 200,
            "content_status": content_status,
            "representation_id": representation_id,
        }
        first_doc = documents[0]
        version = gateway.must(
            "GET",
            f"/api/v1/projects/{project_id}/knowledge/documents/"
            f"{first_doc['document_id']}/versions/{first_doc['version_id']}",
        )
        search = gateway.must(
            "POST",
            f"/api/v1/projects/{project_id}/knowledge/search",
            body={"query": "synthetic groundwater evidence", "limit": 20},
        )
        if not search.get("items"):
            raise RuntimeError("Synthetic Knowledge search returned no EvidenceUnit")
        first_hit = search["items"][0]
        gateway.must(
            "GET",
            f"/api/v1/projects/{project_id}/knowledge/documents/"
            f"{first_hit['document_id']}/versions/{first_hit['version_id']}"
            f"/units/{first_hit['unit_id']}",
        )
        result["knowledge_exact_read"] = {
            "version_status": 200,
            "version_unit_count": version.get("unit_count"),
            "search_status": 200,
            "evidence_unit_status": 200,
            "evidence_unit_digest": first_hit.get("content_digest"),
        }

        stage = "browser_flow"
        page_errors = []
        browser_console_errors = []
        browser_api_errors = []
        workspace_statuses = []
        upload_responses = []
        ui_uploads = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda _error: page_errors.append("pageerror"))
            page.on(
                "console",
                lambda message: browser_console_errors.append(message.type)
                if message.type == "error"
                else None,
            )
            page.on(
                "response",
                lambda response: browser_api_errors.append(
                    {"status": response.status, "path": response.url.split("?", 1)[0]}
                )
                if "/api/v1/" in response.url and response.status >= 400
                else None,
            )

            def record_response(response):
                path = urllib.parse.urlsplit(response.url).path
                if path.endswith("/workspace-summary"):
                    workspace_statuses.append(response.status)
                if "/uploads" in path:
                    upload_responses.append(
                        {"method": response.request.method, "path": path, "status": response.status}
                    )
                    if response.request.method == "POST" and path.endswith("/uploads"):
                        try:
                            ui_uploads.append(response.json())
                        except Exception:
                            pass

            page.on("response", record_response)
            page.goto(args.origin.rstrip("/") + "/")
            token_input = page.get_by_label("临时 Bearer Token")
            token_input.fill(token)
            page.get_by_role("button", name="连接并加载项目").click()
            page.get_by_role("button", name="进入工作区").click()
            page.get_by_role("link", name="数据资产").click()
            second_asset_id = result["asset_pagination"]["second_page_first_id"]
            second_asset = page.get_by_text(second_asset_id, exact=True)
            expect(second_asset).to_have_count(0)
            page.get_by_role("button", name="继续加载").click()
            expect(second_asset).to_be_visible(timeout=15000)
            result["browser_assets_pagination"] = {
                "gateway_origin": args.origin,
                "first_page_hides_101st_asset": True,
                "continue_loading_shows_101st_asset": True,
            }

            page.get_by_role("button", name="上传数据").click()
            page.locator("input[type=file]").set_input_files(
                {
                    "name": "g1-browser-upload.csv",
                    "mimeType": "text/csv",
                    "buffer": b"station,value\nG1-BROWSER,12.5\n",
                }
            )
            page.get_by_role("button", name="上传并继续").click()
            page.get_by_label("列定义（JSON）").fill(
                '[{"name":"station","type":"string"},{"name":"value","type":"float64"}]'
            )
            page.get_by_role("button", name="检查并导入").click()
            page.get_by_role("button", name="完成").wait_for(timeout=15000)
            page.get_by_role("button", name="完成").click()
            expect(page.get_by_role("heading", name="数据资产")).to_be_visible()
            if not workspace_statuses or any(status != 200 for status in workspace_statuses):
                raise RuntimeError("Project workspace summary did not remain HTTP 200")
            if not ui_uploads or ui_uploads[-1].get("filename") != "g1-browser-upload.csv":
                raise RuntimeError("Browser did not receive the synthetic upload receipt")
            upload_id = ui_uploads[-1]["upload_id"]
            ui_upload = wait_for_upload(gateway, project_id, upload_id)
            if ui_upload["status"] != "READY" or not ui_upload.get("asset"):
                raise RuntimeError("Browser uploaded CSV did not become a published Asset")
            result["browser_upload_flow"] = {
                "create_status": next(
                    (item["status"] for item in upload_responses
                     if item["method"] == "POST" and item["path"].endswith("/uploads")),
                    None,
                ),
                "content_status": next(
                    (item["status"] for item in upload_responses
                     if item["method"] == "PUT" and item["path"].endswith("/content")),
                    None,
                ),
                "finalize_status": next(
                    (item["status"] for item in upload_responses
                     if item["method"] == "POST" and item["path"].endswith("/finalize")),
                    None,
                ),
                "terminal_status": ui_upload["status"],
                "workspace_summary_statuses": workspace_statuses,
            }

            page.reload()
            token_after_refresh = page.get_by_label("临时 Bearer Token")
            expect(token_after_refresh).to_be_visible()
            if token_after_refresh.input_value():
                raise RuntimeError("Bearer token persisted across browser refresh")
            result["browser_token_refresh"] = {"token_cleared": True}
            browser.close()
        result["browser_page_errors"] = len(page_errors)
        result["browser_console_errors"] = len(browser_console_errors)
        result["browser_api_errors"] = browser_api_errors
        if page_errors or browser_console_errors or browser_api_errors:
            raise RuntimeError("Browser emitted an uncaught error or API error response")
        result["status"] = "PASS"
        result["status_counts"] = {
            "uploads": args.objects,
            "assets": args.objects,
            "knowledge_documents": args.objects,
        }
    except Exception as error:
        result["failed_stage"] = stage
        result["error_type"] = type(error).__name__
        raise
    finally:
        admin(env_file, "token-revoke", "--token-id", token_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "project_id": result.get("project_id"),
                      "objects_per_catalogue": result["objects_per_catalogue"],
                      "browser_assets_pagination": result.get("browser_assets_pagination"),
                      "output": str(args.output)}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
