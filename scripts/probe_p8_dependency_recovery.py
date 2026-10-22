#!/usr/bin/env python3
"""Exercise browser error recovery when the isolated Data service is unavailable."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright


def compose(env_file: Path, *args: str) -> list[str]:
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
        compose(env_file, "exec", "-T", "platform-api", "platform-admin", *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def gateway_status(origin: str, token: str, path: str) -> tuple[int, dict | None]:
    request = urllib.request.Request(
        origin.rstrip("/") + path,
        headers={"Authorization": "Bearer " + token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read())
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return error.code, payload


def wait_data_healthy(env_file: Path, timeout: int = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            compose(env_file, "ps", "--format", "json", "data-api"),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            try:
                rows = [json.loads(line) for line in result.stdout.splitlines() if line]
            except json.JSONDecodeError:
                rows = []
            if rows and rows[0].get("Health") == "healthy":
                return True
        time.sleep(1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env_file = args.env_file.resolve()
    result = {
        "schema_version": 1,
        "run_id": args.run_id,
        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evidence_kind": "data_dependency_outage_and_browser_network_recovery",
        "production_business_data_read": False,
        "status": "FAIL",
    }
    token_id = None
    data_stopped = False
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
        project_request = urllib.request.Request(
            args.origin.rstrip("/") + "/api/v1/projects",
            data=json.dumps({"name": args.run_id + " dependency recovery"}).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Idempotency-Key": args.run_id + "-project",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(project_request, timeout=15) as response:
            project_id = json.loads(response.read())["project_id"]
        result["project_id"] = project_id

        page_errors = []
        browser_api_statuses = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on("pageerror", lambda _error: page_errors.append("pageerror"))
            page.on(
                "response",
                lambda response: (
                    browser_api_statuses.append(response.status)
                    if "/api/v1/" in response.url
                    else None
                ),
            )
            page.goto(args.origin.rstrip("/") + "/")
            page.get_by_label("临时 Bearer Token").fill(token)
            page.get_by_role("button", name="连接并加载项目").click()
            page.get_by_role("button", name="进入工作区").click()
            expect(
                page.get_by_role("heading", name=args.run_id + " dependency recovery")
            ).to_be_visible()

            stopped = subprocess.run(
                compose(env_file, "stop", "data-api"),
                capture_output=True,
                text=True,
                check=False,
            )
            if stopped.returncode:
                raise RuntimeError("Could not stop the isolated Data API")
            data_stopped = True
            api_status, outage_body = gateway_status(
                args.origin, token, f"/api/v1/projects/{project_id}/workspace-summary"
            )
            if api_status != 503:
                raise RuntimeError("Workspace summary did not fail while Data API was stopped")
            if not outage_body or outage_body.get("code") != "DEPENDENCY_UNAVAILABLE":
                raise RuntimeError("Data outage did not return a bounded dependency error")
            page.get_by_role("link", name="数据资产").click()
            retry = page.get_by_role("button", name="重试")
            expect(retry).to_be_visible(timeout=20_000)

            started = subprocess.run(
                compose(env_file, "start", "data-api"),
                capture_output=True,
                text=True,
                check=False,
            )
            if started.returncode or not wait_data_healthy(env_file):
                raise RuntimeError("Isolated Data API did not recover")
            data_stopped = False
            asset_heading = page.get_by_role("heading", name="数据资产")
            try:
                expect(asset_heading).to_be_visible(timeout=3_000)
            except PlaywrightTimeoutError:
                retry = page.get_by_role("button", name="重试")
                expect(retry).to_be_visible(timeout=5_000)
                retry.click(timeout=5_000)
            expect(asset_heading).to_be_visible(timeout=20_000)

            context.set_offline(True)
            offline_result = page.evaluate(
                "async ({project, token}) => { try { const r = await fetch("
                "`/api/v1/projects/${project}/workspace-summary`, "
                "{headers: {Authorization: `Bearer ${token}`}}); return r.status; } "
                "catch (_) { return 'network-error'; } }",
                {"project": project_id, "token": token},
            )
            context.set_offline(False)
            online_result = page.evaluate(
                "async ({project, token}) => { const r = await fetch("
                "`/api/v1/projects/${project}/workspace-summary`, "
                "{headers: {Authorization: `Bearer ${token}`}}); return r.status; }",
                {"project": project_id, "token": token},
            )
            if offline_result != "network-error" or online_result != 200:
                raise RuntimeError("Browser fetch did not recover after network reconnection")
            browser.close()

        if page_errors:
            raise RuntimeError("Browser raised an uncaught page error during recovery")
        if 503 not in browser_api_statuses or 200 not in browser_api_statuses:
            raise RuntimeError("Browser did not observe both dependency failure and recovery")
        result.update(
            {
                "status": "PASS",
                "workspace_summary_outage_status": api_status,
                "workspace_summary_outage_code": outage_body["code"],
                "browser_retry_recovered": True,
                "browser_offline_result": offline_result,
                "browser_reconnected_status": online_result,
                "browser_page_errors": len(page_errors),
                "browser_api_statuses": browser_api_statuses,
            }
        )
    except Exception as error:
        result["error_type"] = type(error).__name__
        raise
    finally:
        if data_stopped:
            subprocess.run(
                compose(env_file, "start", "data-api"),
                capture_output=True,
                text=True,
                check=False,
            )
            wait_data_healthy(env_file)
        if token_id:
            admin(env_file, "token-revoke", "--token-id", token_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "output": str(args.output)}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
