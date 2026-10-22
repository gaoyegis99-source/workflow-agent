#!/usr/bin/env python3
"""Probe OWNER, EDITOR, VIEWER, cross-project, and expired-token boundaries."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


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


def admin(env_file: Path, *args: str, owner_token: str | None = None) -> dict:
    command = compose(env_file, "exec", "-T")
    if owner_token is not None:
        command.extend(["-e", "V7_OWNER_TOKEN=" + owner_token])
    command.extend(["platform-api", "platform-admin", *args])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        detail = completed.stderr.strip().splitlines()
        raise RuntimeError(
            f"platform-admin {args[0]} failed: {detail[-1][:240] if detail else 'unknown error'}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}


def request(origin: str, token: str, method: str, path: str, body=None, key=None):
    headers = {"Authorization": "Bearer " + token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    http_request = urllib.request.Request(
        origin.rstrip("/") + path, data=data, headers=headers, method=method
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(http_request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def new_identity(env_file: Path, name: str, ttl: int = 3600) -> dict:
    principal = admin(env_file, "principal-create", "--name", name)
    issued = admin(
        env_file,
        "token-issue",
        "--principal-id",
        principal["principal_id"],
        "--ttl-seconds",
        str(ttl),
    )
    return {
        "principal_id": principal["principal_id"],
        "token_id": issued["token_id"],
        "token": issued["token"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    env_file = args.env_file.resolve()
    identities = []
    result = {
        "schema_version": 1,
        "run_id": args.run_id,
        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evidence_kind": "synthetic_project_rbac_and_expiry_via_gateway",
        "production_business_data_read": False,
        "status": "FAIL",
    }
    try:
        owner = new_identity(env_file, args.run_id + " owner")
        identities.append(owner)
        editor = new_identity(env_file, args.run_id + " editor")
        identities.append(editor)
        viewer = new_identity(env_file, args.run_id + " viewer")
        identities.append(viewer)
        project_status, project_payload = request(
            args.origin,
            owner["token"],
            "POST",
            "/api/v1/projects",
            {"name": args.run_id + " RBAC project"},
            args.run_id + "-owner-project",
        )
        if project_status != 201:
            raise RuntimeError("OWNER could not create an isolated project")
        project_id = json.loads(project_payload)["project_id"]
        result["project_id"] = project_id
        for identity, role in ((editor, "EDITOR"), (viewer, "VIEWER")):
            admin(
                env_file,
                "member-set",
                "--project-id",
                project_id,
                "--principal-id",
                identity["principal_id"],
                "--role",
                role,
                owner_token=owner["token"],
            )

        read_status, _ = request(
            args.origin, viewer["token"], "GET", f"/api/v1/projects/{project_id}"
        )
        editor_read_status, _ = request(
            args.origin, editor["token"], "GET", f"/api/v1/projects/{project_id}"
        )
        owner_write_status, _ = request(
            args.origin,
            owner["token"],
            "POST",
            f"/api/v1/projects/{project_id}/uploads",
            {
                "filename": "rbac-owner.md",
                "format": "markdown",
                "size": 15,
                "digest": "sha256:" + "1" * 64,
            },
            args.run_id + "-owner-upload",
        )
        editor_write_status, _ = request(
            args.origin,
            editor["token"],
            "POST",
            f"/api/v1/projects/{project_id}/uploads",
            {
                "filename": "rbac-editor.md",
                "format": "markdown",
                "size": 16,
                "digest": "sha256:" + "2" * 64,
            },
            args.run_id + "-editor-upload",
        )
        viewer_write_status, _ = request(
            args.origin,
            viewer["token"],
            "POST",
            f"/api/v1/projects/{project_id}/uploads",
            {
                "filename": "rbac-viewer.md",
                "format": "markdown",
                "size": 16,
                "digest": "sha256:" + "3" * 64,
            },
            args.run_id + "-viewer-upload",
        )
        workspace_status, _ = request(
            args.origin,
            viewer["token"],
            "GET",
            f"/api/v1/projects/{project_id}/workspace-summary",
        )
        second_status, second_payload = request(
            args.origin,
            owner["token"],
            "POST",
            "/api/v1/projects",
            {"name": args.run_id + " isolated other project"},
            args.run_id + "-other-project",
        )
        if second_status != 201:
            raise RuntimeError("OWNER could not create a second isolated project")
        other_project_id = json.loads(second_payload)["project_id"]
        cross_project_status, _ = request(
            args.origin,
            viewer["token"],
            "GET",
            f"/api/v1/projects/{other_project_id}",
        )
        status = {
            "owner_read": 200,
            "editor_read": 200,
            "viewer_read": 200,
            "owner_write": 201,
            "editor_write": 201,
            "viewer_write": 403,
            "viewer_workspace_summary": 200,
        }
        observed = {
            "owner_read": request(
                args.origin, owner["token"], "GET", f"/api/v1/projects/{project_id}"
            )[0],
            "editor_read": editor_read_status,
            "viewer_read": read_status,
            "owner_write": owner_write_status,
            "editor_write": editor_write_status,
            "viewer_write": viewer_write_status,
            "viewer_workspace_summary": workspace_status,
        }
        if observed != status:
            raise RuntimeError("Observed project role permissions differed from expectations")
        if cross_project_status not in {403, 404}:
            raise RuntimeError("Cross-project read was not denied")

        expiring = new_identity(env_file, args.run_id + " expiring viewer", ttl=5)
        identities.append(expiring)
        admin(
            env_file,
            "member-set",
            "--project-id",
            project_id,
            "--principal-id",
            expiring["principal_id"],
            "--role",
            "VIEWER",
            owner_token=owner["token"],
        )
        before_expiry, _ = request(
            args.origin,
            expiring["token"],
            "GET",
            f"/api/v1/projects/{project_id}",
        )
        time.sleep(5.2)
        after_expiry, _ = request(
            args.origin,
            expiring["token"],
            "GET",
            f"/api/v1/projects/{project_id}",
        )
        if before_expiry != 200 or after_expiry != 401:
            raise RuntimeError("Short-lived token did not transition from valid to expired")
        result.update(
            {
                "status": "PASS",
                "observed_role_statuses": observed,
                "cross_project_status": cross_project_status,
                "expired_token": {"before_expiry": before_expiry, "after_expiry": after_expiry},
                "tokens_revoked_in_finally": True,
            }
        )
    except Exception as error:
        result["error_type"] = type(error).__name__
        raise
    finally:
        for identity in identities:
            admin(env_file, "token-revoke", "--token-id", identity["token_id"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "output": str(args.output)}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
