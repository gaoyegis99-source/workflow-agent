"""Run the P5.3 checker in its fixed production container."""

import json
import os
import subprocess

# Direct Python invocations must use the same project Docker boundary as bin/ops.
import sys as _maintenance_sys  # noqa: E402
import tempfile
import tomllib
from pathlib import Path
from pathlib import Path as _MaintenancePath  # noqa: E402

from platform_checkers.open_science import check
from platform_core.application.open_acceptance import compile_coverage

_maintenance_source = next(
    parent
    for parent in _MaintenancePath(__file__).resolve().parents
    if (parent / "scripts/maintenance/project_environment.py").is_file()
)
_maintenance_sys.path.insert(0, str(_maintenance_source))
from scripts.maintenance.project_environment import docker_executable  # noqa: E402


def main():
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    image = "groundwater-v7-checker:" + version
    spec = {
        "profile": "open-science-v1",
        "delivery_class": "EXPLORATORY",
        "requirements": [
            {
                "requirement_id": "data-validity",
                "description": "Technical data validity",
                "necessary": True,
                "checks": ["DATA_VALIDITY"],
            }
        ],
        "required_outputs": [],
        "allow_manual_review": False,
        "max_check_executions": 1,
    }
    candidate = {
        "digest": "candidate",
        "spec_revision": 1,
        "payload": {
            "executions": ["execution"],
            "workflow_outputs": [],
            "artifacts": [
                {
                    "ref": {
                        "project_id": "project",
                        "asset_id": "asset",
                        "version_id": "1",
                        "version_manifest_digest": "sha256:" + "a" * 64,
                    },
                    "manifest": {
                        "representations": [
                            {
                                "representation_id": "result",
                                "digest": "sha256:" + "b" * 64,
                            }
                        ]
                    },
                }
            ],
        },
    }
    plan, _ = compile_coverage(spec, candidate, [], [], [], [])
    request = {"acceptance_spec": spec, "coverage_plan": plan}
    expected = check(request)
    with tempfile.TemporaryDirectory(prefix="v7-p53-check-") as directory:
        root = Path(directory)
        source, output = root / "input", root / "output"
        source.mkdir(mode=0o755)
        output.mkdir(mode=0o777)
        os.chmod(output, 0o777)
        (source / "request.json").write_text(json.dumps(request, ensure_ascii=False))
        os.chmod(source / "request.json", 0o644)
        subprocess.run(
            [
                docker_executable(),
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--cpus",
                "1",
                "--memory",
                "1g",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "-v",
                f"{source}:/input:ro",
                "-v",
                f"{output}:/output:rw",
                image,
            ],
            check=True,
            timeout=130,
        )
        actual = json.loads((output / "check.json").read_text())
        if actual != expected:
            raise RuntimeError("Container checker differs from the independent host invocation")
    inspected = json.loads(
        subprocess.check_output([docker_executable(), "image", "inspect", image], text=True)
    )[0]
    labels = inspected["Config"]["Labels"]
    expected_source = subprocess.check_output(
        [
            os.environ.get("V7_PYTHON", "python"),
            str(Path(__file__).resolve().parents[2] / "scripts/release/release_source_manifest.py"),
            "--digest-only",
        ],
        text=True,
    ).strip()
    if labels.get("org.opencontainers.image.version") != version:
        raise RuntimeError("Checker image version label differs from the workspace release")
    if labels.get("org.opencontainers.image.revision") != expected_source:
        raise RuntimeError("Checker image was not built from the current release source")
    print(
        f"PASS: fixed {version} checker container produced the deterministic open-science report "
        f"({inspected['Id']}, {expected_source})"
    )


if __name__ == "__main__":
    main()
