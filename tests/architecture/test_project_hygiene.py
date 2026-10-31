from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "maintenance" / "check_project_hygiene.py"
SPEC = importlib.util.spec_from_file_location("check_project_hygiene", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_git_baseline_excludes_runtime_and_local_environments() -> None:
    assert (
        MODULE.check_paths(
            [
                "apps/platform-api/src/platform_api/main.py",
                "docs/architecture/product-line-boundary.md",
                "experiments/p1/toil/uv.lock",
            ]
        )
        == []
    )


def test_historical_and_generated_context_cannot_be_tracked() -> None:
    assert MODULE.check_paths(
        [
            "archive/docs/old-plan.md",
            "docs/verification/candidate-report.md",
            "evidence/archive/old-manifest.json",
            "apps/platform-api/src/platform_api/studio_static/index.html",
        ]
    ) == [
        "apps/platform-api/src/platform_api/studio_static/index.html: "
        "TRACKED_RETRIEVAL_POLLUTION:apps/platform-api/src/platform_api/studio_static",
        "archive/docs/old-plan.md: TRACKED_RETRIEVAL_POLLUTION:archive",
        "docs/verification/candidate-report.md: TRACKED_RETRIEVAL_POLLUTION:docs/verification",
        "evidence/archive/old-manifest.json: TRACKED_RETRIEVAL_POLLUTION:evidence",
    ]


def test_runtime_and_environment_paths_cannot_be_tracked() -> None:
    assert MODULE.check_paths(
        [
            ".p8-runtime/instances/production-final/p8.env",
            "runtimes/cwl/.venv/bin/python",
            "apps/platform-api/node_modules/react/index.js",
            ".env.production",
        ]
    ) == [
        ".env.production: TRACKED_SECRET_CONFIGURATION",
        ".p8-runtime/instances/production-final/p8.env: TRACKED_EPHEMERAL_PATH:.p8-runtime",
        "apps/platform-api/node_modules/react/index.js: TRACKED_EPHEMERAL_PATH:node_modules",
        "runtimes/cwl/.venv/bin/python: TRACKED_EPHEMERAL_PATH:.venv",
    ]


def test_active_acceptance_sources_must_remain_real_project_files(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(
        "\n".join(sorted(MODULE.REQUIRED_IGNORE_RULES)) + "\n", encoding="utf-8"
    )
    (tmp_path / ".ignore").write_text(
        "\n".join(sorted(MODULE.REQUIRED_RETRIEVAL_IGNORE_RULES)) + "\n",
        encoding="utf-8",
    )

    violations = MODULE.check(tmp_path, tracked_paths=[])

    assert violations == [
        f"{relative}: REQUIRED_ACCEPTANCE_SOURCE_MISSING"
        for relative in sorted(MODULE.REQUIRED_ACCEPTANCE_SOURCES)
    ]
