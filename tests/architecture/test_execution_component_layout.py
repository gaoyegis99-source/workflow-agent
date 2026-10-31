import ast
from pathlib import Path

from platform_execution.api import create_app

from scripts.maintenance.check_architecture_boundaries import _configuration_violations, scan
from tests.pathing import SOURCE_ROOT

APPLICATION = SOURCE_ROOT / "apps/execution-service/src/platform_execution/application"
COMPONENTS = APPLICATION / "components"


def class_methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    return {
        item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_execution_facades_remain_small_and_algorithm_free():
    runner = APPLICATION / "runner.py"
    workflow = APPLICATION / "workflow_runner.py"
    assert len(runner.read_text(encoding="utf-8").splitlines()) <= 260
    assert len(workflow.read_text(encoding="utf-8").splitlines()) <= 100
    assert class_methods(runner, "Runner") == {
        "__init__",
        "step",
        "tick",
    }
    assert class_methods(workflow, "WorkflowRunner") == {
        "__init__",
        "bind_lease",
        "step",
    }


def test_execution_components_are_responsibility_scoped():
    required = {
        "materialization.py",
        "collection.py",
        "container_diagnostics.py",
        "publication.py",
        "finalization.py",
        "drivers.py",
        "standard_collection.py",
        "protocols.py",
    }
    assert required <= {path.name for path in COMPONENTS.glob("*.py")}
    assert not (APPLICATION / "components.py").exists()
    for path in COMPONENTS.glob("*.py"):
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 600, path.name


def test_execution_api_keeps_liveness_routes_outside_the_composition_facade():
    api = (SOURCE_ROOT / "apps/execution-service/src/platform_execution/api.py").read_text(
        encoding="utf-8"
    )
    health = (
        SOURCE_ROOT / "apps/execution-service/src/platform_execution/api_health.py"
    ).read_text(encoding="utf-8")
    assert "register_health_routes(" in api
    assert "def health():" not in api
    assert "def ready():" not in api
    assert "def health():" in health
    assert "def ready():" in health


def test_execution_api_composes_action_and_workflow_routes_without_owning_them():
    api = (SOURCE_ROOT / "apps/execution-service/src/platform_execution/api.py").read_text(
        encoding="utf-8"
    )
    actions = (
        SOURCE_ROOT / "apps/execution-service/src/platform_execution/api_actions.py"
    ).read_text(encoding="utf-8")
    workflows = (
        SOURCE_ROOT / "apps/execution-service/src/platform_execution/api_workflows.py"
    ).read_text(encoding="utf-8")

    assert len(api.splitlines()) <= 180
    assert "register_action_routes(app, context)" in api
    assert "register_workflow_routes(app, context)" in api
    assert "def submit(" not in api
    assert "def node(" not in api
    assert "@app.post" in actions and "authenticate_platform" in actions
    assert "@app.post" in workflows and "authenticate_workflow" in workflows
    assert "from platform_execution.api import" not in actions
    assert "from platform_execution.api import" not in workflows


def test_execution_composition_retains_the_internal_route_manifest_without_io():
    app = create_app(database=object(), token="x" * 32)
    paths = {route.path for route in app.routes}

    assert {
        "/healthz",
        "/readyz",
        "/internal/actions/{action}",
        "/internal/actions/{action}/cancel",
        "/internal/projects/{project}/executions/{execution}",
        "/internal/workflows/{execution}/nodes/{node}",
        "/internal/workflows/{execution}/nodes/{node}/trace",
        "/internal/workflows/{execution}/nodes/{node}/files/{representation}",
    } <= paths


def test_execution_components_do_not_reintroduce_generic_callbacks():
    content = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(COMPONENTS.glob("*.py"))
    )
    assert "from collections.abc import Callable" not in content
    assert "self.operation" not in content
    assert "operation: Operation" not in content


def test_execution_algorithms_are_owned_by_concrete_components():
    assert "run" in class_methods(COMPONENTS / "materialization.py", "InputMaterializer")
    assert "run" in class_methods(COMPONENTS / "collection.py", "OutputCollector")
    assert "run" in class_methods(COMPONENTS / "publication.py", "ArtifactPublisher")
    assert "abort" in class_methods(COMPONENTS / "finalization.py", "ExecutionLifecycle")
    assert "run" in class_methods(COMPONENTS / "local_driver.py", "LocalExecutionDriver")
    assert "run" in class_methods(COMPONENTS / "remote_driver.py", "RemoteExecutionDriver")
    assert "run" in class_methods(COMPONENTS / "workflow_driver.py", "WorkflowExecutionDriver")


def test_active_execution_and_platform_boundaries_have_no_bypass_or_cycle():
    assert scan() == []


def test_configuration_boundary_rejects_implicit_environment_catalog_and_release(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(
        "import os\n"
        "value = os.environ.get('V7_RELEASE_DIGEST')\n"
        "configured_catalog()\n"
        "build_catalog()\n"
        "release_headers()\n",
        encoding="utf-8",
    )

    codes = {item["code"] for item in _configuration_violations(path, tmp_path)}

    assert codes == {
        "BUSINESS_ENVIRONMENT_READ",
        "IMPLICIT_CATALOG_LOAD",
        "IMPLICIT_SOURCE_CATALOG",
        "UNINJECTED_RELEASE_CONTEXT",
    }
