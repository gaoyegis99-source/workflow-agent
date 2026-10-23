"""Executable import/package boundaries, including negative controls for the checker."""

import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "platform_core": ROOT / "packages/platform-core/src/platform_core",
    "platform_contracts": ROOT / "packages/contracts/src/platform_contracts",
    "platform_api": ROOT / "apps/platform-api/src/platform_api",
    "platform_infrastructure": ROOT
    / "packages/platform-infrastructure/src/platform_infrastructure",
    "platform_agent_runtime": ROOT / "apps/agent-worker/src/platform_agent_runtime",
}
SOURCES.update(
    {
        "platform_data": ROOT / "apps/data-service/src/platform_data",
        "platform_execution": ROOT / "apps/execution-service/src/platform_execution",
        "platform_knowledge": ROOT / "apps/knowledge-service/src/platform_knowledge",
        "platform_operators": ROOT / "packages/operator-pack/src/platform_operators",
        "platform_checkers": ROOT / "packages/checker-pack/src/platform_checkers",
        "platform_tabular_operators": ROOT
        / "packages/tabular-operator-pack/src/platform_tabular_operators",
    }
)


def test_http_test_client_import_has_no_deprecation_warning():
    result = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", "import fastapi.testclient"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def violations(source: str, module: str, *, is_package: bool = False) -> list[str]:
    tree = ast.parse(source)
    imported = []
    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                package = module if is_package else module.rsplit(".", 1)[0]
                name = importlib.util.resolve_name("." * node.level + name, package)
            imported.append(name)
            # Include `from platform_core import application` as well as the base module.
            imported.extend(f"{name}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"__import__", "eval", "exec"}:
                errors.append("dynamic code/imports bypass dependency checks")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "import_module",
                "spec_from_file_location",
            }:
                errors.append("dynamic imports bypass dependency checks")

    for name in imported:
        top = name.split(".")[0]
        if module.startswith("platform_core"):
            if top not in sys.stdlib_module_names | {"platform_core"}:
                errors.append(f"core depends on {name}")
            if module.startswith("platform_core.domain") and name.startswith(
                "platform_core.application"
            ):
                errors.append(f"domain depends on application: {name}")
        elif module.startswith("platform_contracts"):
            if top not in sys.stdlib_module_names | {"platform_contracts", "pydantic"}:
                errors.append(f"contract depends on {name}")
        elif module.startswith("platform_api"):
            allowed = {
                "platform_api",
                "platform_contracts",
                "platform_core",
                "fastapi",
                "starlette",
                "uvicorn",
                "httpx",
                "yaml",
            }
            if module == "platform_api.app":
                allowed.add("platform_infrastructure")
            if top not in sys.stdlib_module_names | allowed:
                errors.append(f"API has undeclared dependency: {name}")
            if name.startswith("platform_core.domain"):
                errors.append(f"HTTP adapter bypasses application: {name}")
        elif module.startswith("platform_infrastructure"):
            allowed = {
                "platform_infrastructure",
                "platform_contracts",
                "platform_core",
                "sqlalchemy",
                "psycopg",
                "alembic",
                "httpx",
            }
            if top not in sys.stdlib_module_names | allowed:
                errors.append(f"infrastructure depends on forbidden module: {name}")
        elif module.startswith("platform_agent_runtime"):
            allowed = {
                "platform_agent_runtime",
                "platform_contracts",
                "platform_core",
                "platform_infrastructure",
                "langgraph",
                "opentelemetry",
                "psycopg",
            }
            if top not in sys.stdlib_module_names | allowed:
                errors.append(f"agent runtime depends on forbidden module: {name}")
        elif top not in sys.stdlib_module_names and module.startswith(
            (
                "platform_data",
                "platform_execution",
                "platform_knowledge",
                "platform_operators",
                "platform_checkers",
                "platform_tabular_operators",
            )
        ):
            own = module.split(".")[0]
            allowed = {
                "platform_data": {
                    "platform_contracts",
                    "sqlalchemy",
                    "psycopg",
                    "alembic",
                    "fastapi",
                    "uvicorn",
                    "pyarrow",
                },
                "platform_execution": {
                    "platform_contracts",
                    "sqlalchemy",
                    "psycopg",
                    "alembic",
                    "fastapi",
                    "uvicorn",
                    "httpx",
                },
                "platform_knowledge": {
                    "platform_contracts",
                    "sqlalchemy",
                    "psycopg",
                    "alembic",
                    "fastapi",
                    "uvicorn",
                    "httpx",
                },
                "platform_operators": {"numpy", "pyarrow"},
                "platform_checkers": {"pyarrow"},
                "platform_tabular_operators": {"pyarrow"},
            }[own]
            # The fixed spatial runtime is the adapter that owns GDAL/QGIS. Data,
            # Execution, domain and every other operator module remain isolated
            # from those system SDKs.
            if module == "platform_operators.spatial":
                allowed |= {"osgeo", "qgis", "processing"}
            elif module == "platform_checkers.spatial":
                allowed.add("osgeo")
            elif module == "platform_execution.adapters.parquet":
                allowed.add("pyarrow")
            if ".application." in module and (
                top in {"sqlalchemy", "psycopg", "alembic", "fastapi", "httpx"}
                or name.startswith(own + ".adapters")
            ):
                errors.append(f"application depends on adapter: {name}")
            if top not in allowed | {own}:
                errors.append(f"service/algorithm boundary violation: {name}")
            if ".domain." in module and (
                top != own or name.startswith((own + ".application", own + ".adapters"))
            ):
                errors.append(f"domain has external dependency: {name}")
    return errors


def test_all_source_imports_follow_layer_boundaries():
    errors = []
    for package, directory in SOURCES.items():
        for path in directory.rglob("*.py"):
            parts = list(path.relative_to(directory).with_suffix("").parts)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts.pop()
            module = ".".join([package, *parts])
            errors.extend(
                f"{path.relative_to(ROOT)}: {error}"
                for error in violations(path.read_text(), module, is_package=is_package)
            )
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize(
    "source,module",
    [
        ("from fastapi import FastAPI", "platform_core.domain.task"),
        ("from pydantic import BaseModel", "platform_core.domain.task"),
        ("from platform_api import app", "platform_core.application.task"),
        (
            "from platform_core.application.health import read_process_health",
            "platform_core.domain.x",
        ),
        ("from ..application import health", "platform_core.domain.x"),
        ("from platform_core import application", "platform_core.domain.x"),
        ("from platform_core.domain import health", "platform_api.adapters.http.x"),
        ("from platform_api import app", "platform_contracts.v1"),
        ("import openai", "platform_core.domain.x"),
        ("import sqlalchemy", "platform_core.application.x"),
        ("import groundwater_platform_v6", "platform_api.app"),
        ("__import__('fastapi')", "platform_core.domain.x"),
        ("importlib.import_module('fastapi')", "platform_core.domain.x"),
        ("import platform_infrastructure", "platform_core.application.x"),
        ("from platform_infrastructure import database", "platform_api.adapters.http.x"),
        ("import platform_api", "platform_infrastructure.worker"),
        ("from tests.p2.peer_client import PeerClient", "platform_infrastructure.worker"),
        ("import platform_operators", "platform_checkers.reference"),
        ("import numpy", "platform_checkers.reference"),
        ("import platform_data.adapters.tables", "platform_execution.worker"),
        ("import sqlalchemy", "platform_execution.domain.state"),
        ("import sqlalchemy", "platform_data.application.data"),
        ("from platform_execution.adapters import docker", "platform_execution.application.runner"),
        ("from platform_data.adapters import tables", "platform_data.domain.model"),
    ],
)
def test_boundary_checker_rejects_forbidden_dependencies(source, module):
    assert violations(source, module)


def test_core_boundary_accepts_python_standard_library_modules():
    assert not violations("import itertools", "platform_core.application.plan")


def test_only_fixed_spatial_runtime_may_import_system_gdal_qgis_sdks():
    source = "from osgeo import gdal\nfrom qgis.core import QgsApplication"
    assert not violations(source, "platform_operators.spatial")
    assert not violations("from osgeo import ogr", "platform_checkers.spatial")
    assert violations("from qgis.core import QgsApplication", "platform_checkers.spatial")
    assert violations("from qgis.core import QgsApplication", "platform_checkers.reference")
    assert violations("from osgeo import gdal", "platform_operators.reference")
    assert violations("from qgis.core import QgsApplication", "platform_data.application.spatial")


def test_only_execution_parquet_adapter_may_import_pyarrow():
    assert not violations("import pyarrow.parquet", "platform_execution.adapters.parquet")
    assert violations("import pyarrow.parquet", "platform_execution.application.runner")


def test_package_dependencies_and_workspace_stay_inside_v7():
    root_config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    uv_config = root_config["tool"]["uv"]
    members = uv_config["workspace"]["members"]
    # Every declared package is checked; additions cannot silently evade boundary tests.
    declared = {path.parent.parent.relative_to(ROOT).as_posix() for path in SOURCES.values()}
    assert set(members) == declared
    assert all(source == {"workspace": True} for source in uv_config["sources"].values())
    for member in members:
        assert (ROOT / member).resolve().is_relative_to(ROOT)
    core = tomllib.loads((ROOT / "packages/platform-core/pyproject.toml").read_text())
    assert core["project"]["dependencies"] == []
    contracts = tomllib.loads((ROOT / "packages/contracts/pyproject.toml").read_text())
    assert contracts["project"]["dependencies"] == ["pydantic>=2.11,<3"]

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    for package in lock["package"]:
        source = package["source"]
        if "registry" in source:
            assert source == {"registry": "https://pypi.org/simple"}
        else:
            assert set(source) <= {"editable", "virtual"}
            for path in source.values():
                assert not Path(path).is_absolute()
                assert (ROOT / path).resolve().is_relative_to(ROOT)


def test_no_legacy_runtime_paths_or_environment_loading():
    files = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for directory in [ROOT / "apps", ROOT / "packages"]:
        files.extend(directory.rglob("*.py"))
        files.extend(directory.rglob("pyproject.toml"))
    for path in files:
        content = path.read_text().lower()
        for forbidden in [
            "projects/platform/groundwater/v6",
            "groundwater_platform_v6",
            "sys.path",
            "dotenv",
        ]:
            assert forbidden not in content, f"{path}: {forbidden}"
        assert not path.is_symlink(), f"source symlink is not self-contained: {path}"


def test_p3_preserves_delivered_migrations_and_unchanged_third_party_lock_entries():
    baseline = json.loads((ROOT / "docs/verification/p3-baseline.json").read_text())
    locked = {p["name"]: p for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    # Starlette 1.7.0 removes the AnyIO alias import that warned under 4.15.x.
    assert locked["starlette"]["version"] == "1.7.0"
    for name, entry in baseline["third_party"].items():
        if name == "starlette":
            continue
        encoded = json.dumps(locked[name], sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(encoded).hexdigest() == entry["lock_sha256"], name
    for name, digest in baseline["migrations"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
