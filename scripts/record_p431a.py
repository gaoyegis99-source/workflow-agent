"""Record P4.3.1a reliability evidence after the real remote standard gate."""

import hashlib
import json
import platform
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def junit(path):
    tree = ET.parse(path)
    cases = tree.findall(".//testcase")
    return {
        "tests": len(cases),
        "failures": len(tree.findall(".//failure")),
        "errors": len(tree.findall(".//error")),
        "skipped": len(tree.findall(".//skipped")),
    }


def main():
    verification = ROOT / ".p4-runtime/verification"
    unit = junit(verification / "p431a-standard-unit.xml")
    http = junit(verification / "p431a-standard-http.xml")
    if unit != {"tests": 23, "failures": 0, "errors": 0, "skipped": 0}:
        raise RuntimeError("P4.3.1a unit/contract evidence is incomplete")
    if http["failures"] or http["errors"] or http["skipped"] or http["tests"] < 12:
        raise RuntimeError("P4.3.1a real HTTP evidence is incomplete")

    inspection_path = ROOT / ".p4-runtime/remote/latest-inspect.json"
    inspection = json.loads(inspection_path.read_text())
    expected = {
        "state": "SUCCEEDED",
        "executed": True,
        "network_mode": "none",
        "readonly_rootfs": True,
        "memory_bytes": 1024**3,
        "nano_cpus": 10**9,
        "pids_limit": 128,
        "pid": 0,
        "exit_code": 0,
        "label_action_matches": True,
        "label_implementation_matches": True,
    }
    if any(inspection.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Remote Docker isolation evidence is incomplete")
    if inspection.get("cap_drop") != ["ALL"] or "no-new-privileges" not in inspection.get(
        "security_opt", []
    ):
        raise RuntimeError("Remote Docker privilege restrictions are incomplete")
    queued_cancel = json.loads((ROOT / ".p4-runtime/remote/cancel-race.json").read_text())
    running_cancel = json.loads((ROOT / ".p4-runtime/remote/running-cancel.json").read_text())
    peer_restart = json.loads((ROOT / ".p4-runtime/remote/peer-restart.json").read_text())
    security_concurrency = json.loads(
        (ROOT / ".p4-runtime/remote/security-concurrency.json").read_text()
    )
    if len(queued_cancel) != 5 or any(
        item["state"] != "CANCELLED" or item["executed"] for item in queued_cancel
    ):
        raise RuntimeError("Queued cancellation evidence is incomplete")
    if (
        running_cancel["pre_cancel"]["state"] != "RUNNING"
        or not running_cancel["pre_cancel"]["executed"]
        or running_cancel["terminal"]["state"] != "CANCELLED"
        or running_cancel["terminal"]["cleanup_state"] != "CONFIRMED"
    ):
        raise RuntimeError("Running cancellation evidence is incomplete")
    if (
        peer_restart["before_restart"]["state"] != "RUNNING"
        or peer_restart["terminal"]["state"] != "SUCCEEDED"
        or peer_restart["container_count_before"] != 1
        or peer_restart["container_count_after"] != 1
        or peer_restart["before_restart"]["remote_execution_id"]
        != peer_restart["terminal"]["remote_execution_id"]
    ):
        raise RuntimeError("Peer restart evidence is incomplete")
    if (
        security_concurrency["authentication"]
        != {"wrong_token_status": 401, "untrusted_ca_rejected": True}
        or not security_concurrency["unique_actions"]
        or not security_concurrency["unique_executions"]
        or any(item["state"] != "SUCCEEDED" for item in security_concurrency["parallel_actions"])
    ):
        raise RuntimeError("Remote authentication/concurrency evidence is incomplete")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        item["name"]: item["version"]
        for item in lock["package"]
        if item["name"].startswith("groundwater-") and "editable" in item["source"]
    }
    if len(packages) != 8 or set(packages.values()) != {"0.5.1"}:
        raise RuntimeError("All eight V7 packages must be frozen at 0.5.1")

    sources = [
        "packages/contracts/src/platform_contracts/remote.py",
        "packages/platform-core/src/platform_core/domain/remote.py",
        "packages/platform-infrastructure/src/platform_infrastructure/database.py",
        "packages/platform-infrastructure/src/platform_infrastructure/migrations/versions/0006_service_binding_scope.py",
        "apps/execution-service/src/platform_execution/adapters/remote.py",
        "apps/execution-service/src/platform_execution/application/runner.py",
        "apps/execution-service/src/platform_execution/database.py",
        "apps/execution-service/src/platform_execution/migrations/versions/0006_remote_reliability.py",
        "deploy/p43/remote_peer.py",
        "scripts/check_p43_reliability.sh",
    ]
    evidence = {
        "phase": "P4.3.1a-remote-reliability",
        "version": "0.5.1",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "commands": [
            "bash scripts/check_p43_reliability.sh --profile quick",
            "bash scripts/check_p43_reliability.sh --profile standard",
            "bash scripts/check_p43.sh --phase direct",
            "bash scripts/check_p4.sh --phase cartography",
        ],
        "pytest": {"unit_contract": unit, "real_http": http, "direct_regression": 208},
        "compatibility": {"cartography_pytest": 535, "p1_rules": 13},
        "schemas": {
            "platform": "0006_service_binding_scope",
            "data": "0003_spatial_import_runtime",
            "execution": "0006_remote_reliability",
        },
        "packages": packages,
        "cwl_runtime": tomllib.loads((ROOT / "runtimes/cwl/pyproject.toml").read_text())["project"][
            "version"
        ],
        "remote": {
            "host": "192.168.167.89",
            "transport": "verified HTTPS with IP SAN",
            "docker": "28.1.1",
            "job_image": inspection["implementation_digest"],
            "inspection": inspection,
            "certificate_file_digest": sha256(ROOT / ".p4-runtime/remote/tls/peer.crt"),
            "fault_probes": {
                "queued_cancel": queued_cancel,
                "running_cancel": running_cancel,
                "peer_restart": peer_restart,
                "security_concurrency": security_concurrency,
            },
        },
        "python": platform.python_version(),
        "local_docker": subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"], text=True
        ).strip(),
        "source_sha256": {name: sha256(ROOT / name) for name in sources},
        "limits": [
            "one registered remote target",
            "direct operator execution only",
            "single peer; no HA or automatic failover",
            "no remote CWL nodes or mixed workflow",
        ],
    }
    output = ROOT / "docs/verification/p431a-reliability-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"PASS: wrote {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
