#!/usr/bin/env python3
"""Static guardrails for active Platform and Execution ownership boundaries."""

import ast
import json
import re
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
APPLICATION = SOURCE_ROOT / "packages/platform-core/src/platform_core/application"
EXECUTION = SOURCE_ROOT / "apps/execution-service/src/platform_execution/application"
FRONTEND_FEATURES = SOURCE_ROOT / "apps/capability-studio/src/features"
AUTHORING_DOMAIN = SOURCE_ROOT / "packages/platform-core/src/platform_core/domain"
INFRASTRUCTURE_REPOSITORIES = (
    SOURCE_ROOT / "packages/platform-infrastructure/src/platform_infrastructure/repositories"
)
REPOSITORY_TABLE_MODULES = {
    "access.py": frozenset({"platform_infrastructure.tables_access"}),
    "actions.py": frozenset({"platform_infrastructure.tables_access"}),
    "agent_budgets.py": frozenset({"platform_infrastructure.tables_agent"}),
    "agent_conversations.py": frozenset({"platform_infrastructure.tables_agent"}),
    "agent_recovery.py": frozenset({"platform_infrastructure.tables_agent"}),
    "agent_runs.py": frozenset(
        {
            "platform_infrastructure.tables_access",
            "platform_infrastructure.tables_agent",
        }
    ),
    "agent_tools.py": frozenset({"platform_infrastructure.tables_agent"}),
    "authoring.py": frozenset({"platform_infrastructure.tables_workflows"}),
    "budgets.py": frozenset({"platform_infrastructure.tables_access"}),
    "delivery.py": frozenset({"platform_infrastructure.tables_access"}),
    "research.py": frozenset({"platform_infrastructure.tables_research"}),
    "science.py": frozenset(
        {
            "platform_infrastructure.tables_acceptance",
            "platform_infrastructure.tables_access",
        }
    ),
    "service_bindings.py": frozenset({"platform_infrastructure.tables_workflows"}),
    "tasks.py": frozenset({"platform_infrastructure.tables_access"}),
    "workflows.py": frozenset({"platform_infrastructure.tables_workflows"}),
}
# These public services are deliberately composition-only facades.  Their
# responsibility modules may depend on shared ports and domain/application
# helpers, but never back on the facade that assembles them.  Keeping this
# registry local makes a new backwards import (including one hidden inside a
# function) fail the same way as an ordinary import cycle.
SERVICE_FACADE_HELPERS = {
    "control.py": frozenset(
        {
            "control_admission",
            "control_commands",
            "control_grants",
            "control_queries",
        }
    ),
    "science.py": frozenset({"science_support", "science_execution", "science_acceptance"}),
    "workflows.py": frozenset(
        {
            "workflow_support",
            "workflow_packages",
            "workflow_submission",
            "workflow_nodes",
            "workflow_processor",
        }
    ),
    "authoring_release.py": frozenset(
        {
            "authoring_release_support",
            "authoring_candidates",
            "authoring_trials",
            "authoring_releases",
        }
    ),
}
CONFIGURATION_BOUNDARIES = (
    "packages/platform-core/src/platform_core/application",
    "apps/execution-service/src/platform_execution/application",
    "apps/execution-service/src/platform_execution/adapters",
    "apps/data-service/src/platform_data",
    "apps/knowledge-service/src/platform_knowledge",
    "apps/platform-api/src/platform_api",
    "apps/agent-worker/src/platform_agent_runtime",
    "packages/platform-infrastructure/src/platform_infrastructure/services.py",
    "packages/platform-infrastructure/src/platform_infrastructure/database.py",
    "packages/platform-infrastructure/src/platform_infrastructure/worker.py",
    "packages/contracts/src/platform_contracts/health.py",
)
COMPOSITION_MODULES = frozenset({"composition.py"})
_TS_IMPORT = re.compile(r"^\s*import(?:[\s\S]*?from\s*)?[\"']([^\"']+)[\"']", re.MULTILINE)


def _application_module_name(path: Path, application: Path) -> str:
    """Return the full application-relative module name for ``path``."""

    relative = path.relative_to(application).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _application_imports(path: Path, application: Path = APPLICATION) -> set[str]:
    """Find application-module imports, including local function imports.

    A previous guard collapsed each target to its first path component.  That
    missed cycles once application code gained responsibility-specific modules
    and made relative imports invisible.  This deliberately reads the whole
    AST so deferred imports have the same architecture contract as top-level
    imports.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    prefix = "platform_core.application."
    current = _application_module_name(path, application)
    current_package = current.split(".")[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                # ``from .x`` resolves inside the current package.  Every
                # application module is represented relative to
                # ``platform_core.application`` so the result can be matched
                # directly against the known module set below.
                climb = node.level - 1
                if climb > len(current_package):
                    continue
                base = current_package[: len(current_package) - climb]
                if node.module:
                    imports.add(".".join([*base, *node.module.split(".")]))
                else:
                    imports.update(".".join([*base, alias.name]) for alias in node.names)
            elif node.module and node.module.startswith(prefix):
                imports.add(node.module.removeprefix(prefix))
            elif node.module == "platform_core.application":
                imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name.startswith(prefix):
                    imports.add(item.name.removeprefix(prefix))
    return imports


def _application_cycle_violations(application: Path) -> list[dict[str, object]]:
    """Report strongly connected application modules at their actual granularity."""

    paths = [path for path in application.rglob("*.py") if "__pycache__" not in path.parts]
    modules = {
        _application_module_name(path, application): path
        for path in paths
        if _application_module_name(path, application)
    }
    edges = {
        module: _application_imports(path, application) & modules.keys()
        for module, path in modules.items()
    }
    return [
        {"code": "APPLICATION_IMPORT_CYCLE", "modules": group}
        for group in _strongly_connected(edges)
    ]


def _imports_infrastructure(tree: ast.AST) -> set[str]:
    """Return concrete Platform Infrastructure imports from an application AST."""

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(
                item.name
                for item in node.names
                if item.name == "platform_infrastructure"
                or item.name.startswith("platform_infrastructure.")
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "platform_infrastructure" or node.module.startswith(
                "platform_infrastructure."
            ):
                imports.add(node.module)
    return imports


def _application_service_violations(application: Path, source_root: Path) -> list[dict[str, str]]:
    """Enforce composition-only service facades and port-only application code."""

    findings: list[dict[str, str]] = []
    for path in sorted(application.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(source_root).as_posix()
        for target in sorted(_imports_infrastructure(tree)):
            findings.append(
                {
                    "code": "APPLICATION_INFRASTRUCTURE_IMPORT",
                    "path": relative,
                    "target": target,
                }
            )

    for facade_name, helpers in SERVICE_FACADE_HELPERS.items():
        facade = application / facade_name
        if not facade.is_file():
            continue
        facade_module = facade.stem
        facade_tree = ast.parse(facade.read_text(encoding="utf-8"), filename=str(facade))
        for class_node in (node for node in facade_tree.body if isinstance(node, ast.ClassDef)):
            for member in class_node.body:
                is_method = isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                if is_method and member.name != "__init__":
                    findings.append(
                        {
                            "code": "APPLICATION_FACADE_IMPLEMENTATION",
                            "path": facade.relative_to(source_root).as_posix(),
                            "member": member.name,
                        }
                    )
        for helper in sorted(helpers):
            helper_path = application / f"{helper}.py"
            if not helper_path.is_file():
                findings.append(
                    {
                        "code": "APPLICATION_FACADE_HELPER_MISSING",
                        "path": facade.relative_to(source_root).as_posix(),
                        "target": helper,
                    }
                )
                continue
            if facade_module in _application_imports(helper_path, application):
                findings.append(
                    {
                        "code": "APPLICATION_FACADE_BACK_IMPORT",
                        "path": helper_path.relative_to(source_root).as_posix(),
                        "target": facade_module,
                    }
                )
    return findings


def _strongly_connected(edges: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    pending: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    groups: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        pending.add(node)
        for child in sorted(edges.get(node, ())):
            if child not in edges:
                continue
            if child not in indexes:
                visit(child)
                lowlinks[node] = min(lowlinks[node], lowlinks[child])
            elif child in pending:
                lowlinks[node] = min(lowlinks[node], indexes[child])
        if lowlinks[node] == indexes[node]:
            group = []
            while True:
                child = stack.pop()
                pending.remove(child)
                group.append(child)
                if child == node:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    for node in sorted(edges):
        if node not in indexes:
            visit(node)
    return sorted(groups)


def _repository_receiver(node: ast.expr) -> bool:
    """Recognize ``repo``/``repository`` whether stored on self or locally."""

    if isinstance(node, ast.Name):
        return node.id in {"repo", "repository"}
    return isinstance(node, ast.Attribute) and node.attr in {"repo", "repository"}


def _phase_assignment(target: ast.expr) -> bool:
    """Return true for an in-place ``runtime['phase']`` write."""

    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == "phase"
        and isinstance(target.value, ast.Subscript)
        and isinstance(target.value.slice, ast.Constant)
        and target.value.slice.value == "runtime"
    )


def _execution_violations(path: Path) -> list[dict[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative = path.relative_to(SOURCE_ROOT).as_posix()
    findings: list[dict[str, str]] = []
    is_runtime = path == EXECUTION / "execution_runtime.py"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_phase_assignment(target) for target in targets):
                findings.append({"code": "DIRECT_EXECUTION_PHASE_MUTATION", "path": relative})
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if node.func.attr == "transition" and _repository_receiver(receiver) and not is_runtime:
            findings.append({"code": "DIRECT_EXECUTION_TRANSITION", "path": relative})
        if node.func.attr != "change" or not _repository_receiver(receiver):
            continue
        for keyword in node.keywords:
            if keyword.arg in {"state", "executed", "phase"}:
                findings.append({"code": "DIRECT_EXECUTION_LIFECYCLE_WRITE", "path": relative})
                break
            if keyword.arg == "runtime" and any(
                isinstance(item, ast.Dict)
                and any(isinstance(key, ast.Constant) and key.value == "phase" for key in item.keys)
                for item in ast.walk(keyword.value)
            ):
                findings.append({"code": "DIRECT_EXECUTION_PHASE_WRITE", "path": relative})
                break
    return findings


def _attribute_parts(node: ast.expr) -> tuple[str, ...]:
    """Return a simple dotted expression without trying to evaluate code."""

    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return tuple(reversed(parts))
    return ()


def _configuration_violations(path: Path, source_root: Path) -> list[dict[str, str]]:
    """Keep deployment I/O at composition roots, not application boundaries.

    The check intentionally targets the active business layers rather than all
    Python modules: a composition root is permitted to read its process
    environment, while an adapter may still use ``os.fsync`` or ``os.getuid``
    for its own local operation.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative = path.relative_to(source_root).as_posix()
    findings: list[dict[str, str]] = []
    catalog_definition = path.name in {"catalog.py", "checker_catalog.py", "model_catalog.py"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts = _attribute_parts(node)
            if parts[:2] == ("os", "environ"):
                findings.append({"code": "BUSINESS_ENVIRONMENT_READ", "path": relative})
        elif isinstance(node, ast.Call):
            parts = _attribute_parts(node.func)
            if parts == ("os", "getenv"):
                findings.append({"code": "BUSINESS_ENVIRONMENT_READ", "path": relative})
            if parts and parts[-1] == "configured_catalog":
                findings.append({"code": "IMPLICIT_CATALOG_LOAD", "path": relative})
            if (
                not catalog_definition
                and parts
                and parts[-1] in {"build_catalog", "build_checker_catalog"}
            ):
                findings.append({"code": "IMPLICIT_SOURCE_CATALOG", "path": relative})
            if (
                parts
                and parts[-1]
                in {
                    "release_headers",
                    "ReleaseIdentityMiddleware",
                    "production_release",
                }
                and not any(keyword.arg == "release" for keyword in node.keywords)
            ):
                findings.append({"code": "UNINJECTED_RELEASE_CONTEXT", "path": relative})
        elif isinstance(node, ast.Name) and node.id == "configured_catalog":
            findings.append({"code": "IMPLICIT_CATALOG_LOAD", "path": relative})
    # Multiple AST nodes can identify the same unsafe expression.  Keep the
    # report stable and useful rather than reporting implementation details.
    return [dict(item) for item in {tuple(sorted(item.items())) for item in findings}]


def _frontend_feature_violations(features: Path, source_root: Path) -> list[dict[str, str]]:
    """Features may consume core/shared layers, never another feature's internals."""

    findings: list[dict[str, str]] = []
    for path in sorted(features.rglob("*.ts*")):
        if path.name.endswith(".d.ts") or "node_modules" in path.parts:
            continue
        relative = path.relative_to(source_root).as_posix()
        source_feature = path.relative_to(features).parts[0]
        for target in _TS_IMPORT.findall(path.read_text(encoding="utf-8")):
            if not target.startswith("."):
                continue
            resolved = (path.parent / target).resolve()
            try:
                target_feature = resolved.relative_to(features.resolve()).parts[0]
            except ValueError:
                continue
            if target_feature != source_feature:
                findings.append(
                    {
                        "code": "FRONTEND_FEATURE_REVERSE_IMPORT",
                        "path": relative,
                        "target": target,
                    }
                )
    return findings


def _repository_table_imports(path: Path) -> set[str]:
    """Return direct imports of table declaration modules, including aliases."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "platform_infrastructure":
                imported.update(f"platform_infrastructure.{alias.name}" for alias in node.names)
            else:
                imported.add(node.module)
    return {
        target
        for target in imported
        if target == "platform_infrastructure.tables"
        or target.startswith("platform_infrastructure.tables_")
    }


def _repository_table_violations(source_root: Path) -> list[dict[str, str]]:
    repository_root = source_root / INFRASTRUCTURE_REPOSITORIES.relative_to(SOURCE_ROOT)
    if not repository_root.is_dir():
        return []
    findings: list[dict[str, str]] = []
    for path in sorted(repository_root.glob("*.py")):
        if path.name not in REPOSITORY_TABLE_MODULES:
            continue
        allowed = REPOSITORY_TABLE_MODULES[path.name]
        relative = path.relative_to(source_root).as_posix()
        for target in sorted(_repository_table_imports(path)):
            if target == "platform_infrastructure.tables":
                findings.append(
                    {
                        "code": "REPOSITORY_TABLE_AGGREGATE_IMPORT",
                        "path": relative,
                        "target": target,
                    }
                )
            elif not any(target == module or target.startswith(f"{module}.") for module in allowed):
                findings.append(
                    {
                        "code": "REPOSITORY_TABLE_OWNERSHIP_IMPORT",
                        "path": relative,
                        "target": target,
                    }
                )
    return findings


def _authoring_graph_violations(source_root: Path) -> list[dict[str, str]]:
    """Keep the public graph import path as an export-only boundary.

    Callers retain ``platform_core.domain.authoring_graph``.  The responsibility
    modules below it may collaborate with one another, but importing that
    façade would recreate an implicit cycle and let implementation code grow
    back into the public module unnoticed.
    """

    domain = source_root / AUTHORING_DOMAIN.relative_to(SOURCE_ROOT)
    facade = domain / "authoring_graph.py"
    expected = {
        "authoring_graph_envelope.py",
        "authoring_graph_structure.py",
        "authoring_graph_preview.py",
        "authoring_graph_advanced.py",
        "authoring_graph_staleness.py",
    }
    findings: list[dict[str, str]] = []
    if not facade.is_file():
        return [{"code": "AUTHORING_GRAPH_FACADE_MISSING", "path": str(facade)}]
    facade_tree = ast.parse(facade.read_text(encoding="utf-8"), filename=str(facade))
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in facade_tree.body):
        findings.append(
            {
                "code": "AUTHORING_GRAPH_FACADE_IMPLEMENTATION",
                "path": facade.relative_to(source_root).as_posix(),
            }
        )
    for name in sorted(expected):
        if not (domain / name).is_file():
            findings.append(
                {
                    "code": "AUTHORING_GRAPH_RESPONSIBILITY_MISSING",
                    "path": facade.relative_to(source_root).as_posix(),
                    "target": name,
                }
            )
    for path in sorted(domain.glob("authoring_graph_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports_facade = any(
            isinstance(node, ast.Import)
            and any(item.name == "platform_core.domain.authoring_graph" for item in node.names)
            or isinstance(node, ast.ImportFrom)
            and (
                node.module == "platform_core.domain.authoring_graph"
                or node.level == 1
                and node.module == "authoring_graph"
            )
            for node in ast.walk(tree)
        )
        if imports_facade:
            findings.append(
                {
                    "code": "AUTHORING_GRAPH_FACADE_BACK_IMPORT",
                    "path": path.relative_to(source_root).as_posix(),
                }
            )
    return findings


def scan(source_root: Path = SOURCE_ROOT) -> list[dict[str, object]]:
    application = source_root / APPLICATION.relative_to(SOURCE_ROOT)
    execution = source_root / EXECUTION.relative_to(SOURCE_ROOT)
    findings: list[dict[str, object]] = _application_cycle_violations(application)
    findings.extend(_application_service_violations(application, source_root))
    for path in execution.rglob("*.py"):
        if "__pycache__" not in path.parts:
            findings.extend(_execution_violations(path))
    callback_paths = (
        execution / "runner.py",
        execution / "workflow_runner.py",
        execution / "execution_runtime.py",
    )
    for path in callback_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        callback = any(
            isinstance(node, ast.Name)
            and node.id == "Callable"
            or isinstance(node, ast.arg)
            and node.arg in {"trace_context", "permit_request"}
            or isinstance(node, ast.keyword)
            and node.arg in {"trace_context", "permit_request"}
            for node in ast.walk(tree)
        )
        if callback:
            findings.append(
                {
                    "code": "GENERIC_EXECUTION_CALLBACK",
                    "path": path.relative_to(source_root).as_posix(),
                }
            )
    for relative in CONFIGURATION_BOUNDARIES:
        boundary = source_root / relative
        paths = [boundary] if boundary.is_file() else sorted(boundary.rglob("*.py"))
        for path in paths:
            if (
                path.name not in {"__init__.py", *COMPOSITION_MODULES}
                and "__pycache__" not in path.parts
            ):
                findings.extend(_configuration_violations(path, source_root))
    frontend_features = source_root / FRONTEND_FEATURES.relative_to(SOURCE_ROOT)
    if frontend_features.is_dir():
        findings.extend(_frontend_feature_violations(frontend_features, source_root))
    findings.extend(_repository_table_violations(source_root))
    findings.extend(_authoring_graph_violations(source_root))
    return sorted(findings, key=lambda item: json.dumps(item, sort_keys=True))


def main() -> None:
    findings = scan()
    print(json.dumps({"status": "PASS" if not findings else "FAILED", "findings": findings}))
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
