"""Record successful check_p2 inputs; called only after every gate succeeds."""

import hashlib
import json
import platform
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from p2_environment import POSTGRES_IMAGE

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = ET.parse(ROOT / ".p2-runtime/verification/tests.xml").getroot()
    suites = report.findall("testsuite")
    counts = {
        key: sum(int(s.get(key, 0)) for s in suites)
        for key in ["tests", "failures", "errors", "skipped"]
    }
    if not counts["tests"] or any(counts[key] for key in ["failures", "errors", "skipped"]):
        raise RuntimeError("P2 cannot pass with failed, missing or skipped tests")
    files = [ROOT / "pyproject.toml", ROOT / "uv.lock", ROOT / ".python-version"]
    for directory in ["apps", "packages", "scripts", "tests", "deploy"]:
        files.extend(
            path
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and path.suffix in {".py", ".toml", ".json", ".yaml", ".sh"}
        )
    result = {
        "phase": "P2",
        "cycle": "followup-0.1.1",
        "schema_version": "0002_observation_snapshot",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "command": "bash scripts/check_p2.sh",
        "pytest": counts,
        "pytest_by_suite": {
            group: sum(
                1
                for case in report.iter("testcase")
                if case.get("classname", "").startswith("tests." + group + ".")
            )
            for group in ["contracts", "architecture", "integration", "p2"]
        },
        "initial_baseline": "p2-initial-evidence.json (158 pytest, 13 P1 rules)",
        "defect_regression_before_fix": {
            "membership_deadlocks": 6,
            "duplicate_observation_stuck": 1,
            "historical_invalid_binding_http_500": 1,
            "invalid_observation_settled": 1,
        },
        "p1_rule_tests": "PASS (unittest discovery; see command output)",
        "distribution": (
            "PASS: four wheels/sdists, isolated install, empty database and 0001 upgrade, "
            "CLI/worker/HTTP including unknown command"
        ),
        "peer_storage": (
            "SQLite WAL/FULL, per-test directory under /dev/shm on Linux; "
            "survives subprocess restart, host power loss not tested"
        ),
        "python": platform.python_version(),
        "postgres_image": POSTGRES_IMAGE,
        "versions": {
            name: version(name)
            for name in [
                "sqlalchemy",
                "psycopg",
                "alembic",
                "fastapi",
                "pydantic",
                "pytest",
                "ruff",
                "groundwater-platform-api",
                "groundwater-platform-core",
                "groundwater-platform-contracts",
                "groundwater-platform-infrastructure",
            ]
        },
        "sources_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files)
        },
        "not_run": ["P1 heavy component probes (adapters unchanged)", "P3-P8 capabilities"],
    }
    (ROOT / "docs/verification/p2-evidence.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"PASS: P2 evidence recorded ({counts['tests']} pytest cases, no skips)")


if __name__ == "__main__":
    main()
