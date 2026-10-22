#!/usr/bin/env python3
"""Read-only production health, Release identity, CSP, and source-map probe."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def get(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {
                "status": response.status,
                "release_digest": response.headers.get("X-V7-Release-Digest"),
                "csp": response.headers.get("Content-Security-Policy"),
                "body": response.read(512).decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": error.code,
            "release_digest": error.headers.get("X-V7-Release-Digest"),
            "csp": error.headers.get("Content-Security-Policy"),
            "body": error.read(512).decode("utf-8", errors="replace"),
        }
    except (OSError, urllib.error.URLError) as error:
        return {"status": None, "error": type(error).__name__}


ERROR_PATTERN = re.compile(
    r"(?i)traceback|error|exception|failed|failure|timeout|unhealthy|schema|migration|panic|critical"
)


def docker_observation() -> dict[str, object]:
    project = "groundwater-v7-p8-production-final"
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.Names}}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return {"project": project, "container_count": None, "error": "docker_unavailable"}
    containers = [line for line in result.stdout.splitlines() if line]
    rows = []
    matching_error_lines = 0
    for name in containers:
        state_result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            state = json.loads(state_result.stdout)
        except (TypeError, ValueError):
            state = {}
        log_result = subprocess.run(
            ["docker", "logs", "--since", "10m", "--timestamps", name],
            check=False,
            capture_output=True,
            text=True,
        )
        log_lines = ((log_result.stdout or "") + "\n" + (log_result.stderr or "")).splitlines()
        matching = sum(bool(ERROR_PATTERN.search(line)) for line in log_lines)
        matching_error_lines += matching
        rows.append(
            {
                "name": name,
                "running": state.get("Running"),
                "status": state.get("Status"),
                "restart_count": state.get("RestartCount", 0),
                "health": (state.get("Health") or {}).get("Status"),
                "log_line_count": len(log_lines),
                "matching_error_lines": matching,
            }
        )
    health_counts = {}
    for row in rows:
        key = row["health"] if row["health"] is not None else "no_health_status"
        health_counts[key] = health_counts.get(key, 0) + 1
    return {
        "project": project,
        "container_count": len(rows),
        "health_counts": health_counts,
        "restart_count_nonzero": sum(row["restart_count"] > 0 for row in rows),
        "recent_log_scan": {
            "window": "10m",
            "matching_error_lines": matching_error_lines,
            "pattern": ERROR_PATTERN.pattern,
        },
        "containers": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loopback", default="http://127.0.0.1:18080")
    parser.add_argument("--lan", default="http://192.168.167.87:18080")
    parser.add_argument(
        "--pointer", type=Path, default=Path("docs/verification/p82-current-release.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pointer = json.loads(args.pointer.read_text(encoding="utf-8"))
    digest = pointer.get("release_digest")
    runtime_observation = docker_observation()
    requests = {}
    for name, origin in (("loopback", args.loopback), ("lan", args.lan)):
        requests[name] = {
            path: get(origin + path)
            for path in ("/api/v1/health", "/api/v1/ready", "/gateway/health", "/")
        }
        requests[name]["/source-map"] = get(origin + "/studio/assets/index-B0fiLP7e.js.map")
    output = {
        "observed_at": datetime.now(UTC).isoformat(),
        "business_data_read": False,
        "business_data_written": False,
        "expected_release_digest": digest,
        "requests": requests,
        "release_digest_matches_pointer": all(
            requests[name]["/api/v1/health"].get("release_digest") == digest
            for name in ("loopback", "lan")
        ),
        "source_map_status": requests["loopback"]["/source-map"].get("status"),
        "root_csp_present": bool(requests["loopback"]["/"].get("csp")),
        "runtime_observation": runtime_observation,
        "running_production_container_count": runtime_observation.get("container_count"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    if not output["release_digest_matches_pointer"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
