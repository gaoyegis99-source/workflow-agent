"""Keep split Platform services as composition roots instead of new god modules."""

from __future__ import annotations

import ast
from pathlib import Path

from platform_core.application.authoring_release import AuthoringReleaseService
from platform_core.application.control import ControlService
from platform_core.application.science import Science
from platform_core.application.workflows import Workflows

from scripts.maintenance.check_architecture_boundaries import (
    _application_cycle_violations,
    _application_service_violations,
    scan,
)
from tests.pathing import SOURCE_ROOT

APPLICATION = SOURCE_ROOT / "packages/platform-core/src/platform_core/application"
FACADES = {
    "control.py": "ControlService",
    "science.py": "Science",
    "workflows.py": "Workflows",
    "authoring_release.py": "AuthoringReleaseService",
}


def _methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    service = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_service_facades_only_assemble_stable_public_services():
    for filename, class_name in FACADES.items():
        path = APPLICATION / filename
        assert _methods(path, class_name) == {"__init__"}
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 80


def test_split_services_preserve_their_public_method_surfaces():
    expected = {
        ControlService: {
            "workspace_summary",
            "execution_summaries",
            "replay_action",
            "create_project",
            "projects",
            "project",
            "create_task",
            "task",
            "tasks",
            "budget",
            "revise",
            "control",
            "grants",
            "grant",
            "revoke_grant",
            "command",
            "submit_action",
            "admit",
        },
        Science: {
            "authorize",
            "data_call",
            "submit",
            "permit",
            "execution_view",
            "permit_publication",
            "activities",
            "cancel",
            "operator_role_binding",
            "candidate",
            "candidates",
            "check_receipt",
            "accept",
            "checks",
            "review",
        },
        Workflows: {
            "register",
            "get",
            "list",
            "admit",
            "submit",
            "submit_authoring_trial",
            "permit_node",
            "observe_node",
            "steps",
        },
        AuthoringReleaseService: {
            "create_candidate",
            "candidate",
            "candidates",
            "create_trial",
            "trial",
            "trials",
            "create_release",
            "release",
            "releases",
            "project_releases",
            "release_lifecycle",
            "transition_release",
            "execute_release",
        },
    }
    for service, methods in expected.items():
        assert all(hasattr(service, method) for method in methods)


def test_agent_coordinator_delegates_read_models_to_a_read_only_component():
    source = (APPLICATION / "agent_kernel.py").read_text(encoding="utf-8")
    views = (APPLICATION / "agent_views.py").read_text(encoding="utf-8")

    assert "self._views = AgentViews(store)" in source
    assert "return self._views.view(" in source
    assert "return self._views.task_view(" in source
    assert "class AgentViews" in views
    assert "model context or checkpoints" in views


def test_active_application_services_use_ports_and_do_not_back_import_facades():
    assert _application_service_violations(APPLICATION, SOURCE_ROOT) == []
    assert scan() == []


def test_service_guard_rejects_back_import_and_infrastructure_dependency(tmp_path: Path):
    application = tmp_path / "packages/platform-core/src/platform_core/application"
    application.mkdir(parents=True)
    (application / "science.py").write_text(
        "class Science:\n    def __init__(self):\n        pass\n",
        encoding="utf-8",
    )
    (application / "science_support.py").write_text(
        "from platform_core.application.science import Science\n"
        "from platform_infrastructure import tables\n",
        encoding="utf-8",
    )
    for name in ("science_execution.py", "science_acceptance.py"):
        (application / name).write_text("pass\n", encoding="utf-8")

    findings = _application_service_violations(application, tmp_path)
    assert {item["code"] for item in findings} == {
        "APPLICATION_FACADE_BACK_IMPORT",
        "APPLICATION_INFRASTRUCTURE_IMPORT",
    }


def test_cycle_guard_includes_imports_hidden_inside_functions(tmp_path: Path):
    application = tmp_path / "packages/platform-core/src/platform_core/application"
    application.mkdir(parents=True)
    (application / "first.py").write_text(
        "def load():\n    from platform_core.application.second import value\n    return value\n",
        encoding="utf-8",
    )
    (application / "second.py").write_text(
        "from platform_core.application.first import load\nvalue = load\n",
        encoding="utf-8",
    )

    assert _application_cycle_violations(application) == [
        {"code": "APPLICATION_IMPORT_CYCLE", "modules": ["first", "second"]}
    ]
