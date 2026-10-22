import json
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.profiles import profile
from platform_contracts.release import (
    REQUIRED_WHEELS,
    file_digest,
    load_release_lock,
    production_release,
)
from platform_contracts.release import digest as release_digest
from platform_contracts.science import OperatorView
from platform_core.application.agent_kernel import create_agent_run
from platform_core.application.catalog import (
    build_catalog,
    load_catalog,
    operators,
    resolve,
    snapshot,
    validate_catalog,
    write_catalog,
)
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.science import Science
from platform_core.application.tool_catalog import build_tool_catalog, tool_spec, write_tool_catalog
from platform_core.domain.control import Identity, RuleViolation
from platform_execution.implementations import (
    CHECKER_IMPLEMENTATIONS,
    COMPUTE_IMPLEMENTATIONS,
    implementation,
)
from platform_execution.operator_catalog import exact_snapshot
from platform_execution.operator_catalog import load as load_execution_catalog
from pydantic import ValidationError


def target(operator_id="timeseries-completeness", implementation_id="p3-completeness-v1"):
    return {
        "kind": "OPERATOR",
        "operator_id": operator_id,
        "version_id": "1.0.0",
        "execution_binding": {"kind": "LOCAL", "implementation_id": implementation_id},
    }


def redigest(value):
    from platform_core.application.catalog import _digest

    value["catalog_digest"] = _digest(
        {key: item for key, item in value.items() if key != "catalog_digest"}
    )
    return value


def wheel_evidence(release_version):
    return {
        f"{name}-{release_version}-py3-none-any.whl": "sha256:" + "9" * 64
        for name in REQUIRED_WHEELS
    }


def studio_evidence():
    root = Path("apps/platform-api/src/platform_api/studio_static")
    return {
        path.relative_to(root).as_posix(): file_digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def reoperator(value, index=0):
    from platform_core.application.catalog import _digest

    operator = value["operators"][index]
    semantic = {
        name: operator[name]
        for name in (
            "operator_id",
            "version_id",
            "ownership",
            "trust_level",
            "inputs",
            "parameters",
            "outputs",
            "effects",
            "supports_workflow",
            "legacy",
        )
    }
    operator["operator_digest"] = _digest(semantic)
    return redigest(value)


def test_catalog_is_deterministic_and_round_trips_for_both_services(tmp_path):
    first = build_catalog()
    second = build_catalog()
    assert first == second
    path = tmp_path / "operator-catalog.json"
    assert write_catalog(path) == first
    assert load_catalog(path) == first
    assert load_execution_catalog(path) == first
    frozen = snapshot(target(), first)
    assert exact_snapshot(first, frozen)["operator_id"] == "timeseries-completeness"


@pytest.mark.parametrize(
    "mutation", ["duplicate_operator", "duplicate_implementation", "floating_image", "resource"]
)
def test_catalog_rejects_ambiguous_or_unbounded_definitions(mutation):
    value = json.loads(json.dumps(build_catalog()))
    if mutation == "duplicate_operator":
        value["operators"].append(value["operators"][0])
    elif mutation == "duplicate_implementation":
        value["operators"][1]["implementations"] = value["operators"][0]["implementations"]
    elif mutation == "floating_image":
        value["operators"][0]["implementations"][0]["image_digest"] = "latest"
    else:
        value["operators"][0]["implementations"][0]["resource_profile"] = "unbounded"
    redigest(value)
    with pytest.raises(RuleViolation):
        validate_catalog(value)


@pytest.mark.parametrize("mutation", ["invalid_port", "deep_contract", "too_many_ports"])
def test_catalog_rejects_adversarial_contract_shapes(mutation):
    value = json.loads(json.dumps(build_catalog()))
    if mutation == "invalid_port":
        value["operators"][0]["inputs"] = {"../observations": "File"}
    elif mutation == "too_many_ports":
        value["operators"][0]["outputs"] = {f"p{index}": "File" for index in range(65)}
    else:
        nested = {"leaf": "string"}
        for _ in range(18):
            nested = {"next": nested}
        value["operators"][0]["parameters"] = nested
    reoperator(value)
    with pytest.raises(RuleViolation):
        validate_catalog(value)


def test_catalog_file_size_is_bounded(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="read"):
        load_catalog(path)
    with pytest.raises(ValueError, match="read"):
        load_execution_catalog(path)


def test_compute_checker_and_service_tools_cannot_cross_runtime_boundaries():
    assert set(COMPUTE_IMPLEMENTATIONS).isdisjoint(CHECKER_IMPLEMENTATIONS)
    compute = target() | {"kind": "OPERATOR"}
    checker = {
        "kind": "CHECK",
        "target": {
            "operator_id": "open-science-check",
            "version_id": "1.0.0",
            "execution_binding": {
                "kind": "LOCAL",
                "implementation_id": "platform-runtime-integrity-v1",
            },
        },
    }
    identity, _ = implementation({"kind": "OPERATOR", "target": compute})
    assert identity == "p3-completeness-v1"
    identity, _ = implementation(checker)
    assert identity == "platform-runtime-integrity-v1"
    with pytest.raises(ValueError):
        implementation({"kind": "CHECK", "target": compute})
    with pytest.raises(ValueError):
        implementation({"kind": "OPERATOR", "target": checker["target"]})
    listed = {item["operator_id"] for item in operators(build_catalog())}
    assert "open-science-check" not in listed
    assert "capability.list_operators" not in listed


def test_lifecycle_controls_new_admission_without_erasing_history():
    active = build_catalog()
    disabled = json.loads(json.dumps(active))
    disabled["operators"][0]["lifecycle"] = "DISABLED"
    redigest(disabled)
    validate_catalog(disabled)
    with pytest.raises(RuleViolation):
        resolve(target(), disabled)
    assert resolve(target(), disabled, admission=False)["lifecycle"] == "DISABLED"
    assert (
        snapshot(target(), disabled, admission=False)["operator_digest"]
        == snapshot(target(), active)["operator_digest"]
    )
    # Lifecycle is a deployment decision: it changes the catalogue/release
    # identity while preserving the immutable scientific definition that old
    # terminal records reference. Operators must drain before this switch.
    assert disabled["catalog_digest"] != active["catalog_digest"]


def test_catalog_mismatch_is_rejected_by_execution():
    platform = build_catalog()
    other = json.loads(json.dumps(platform))
    other["operators"][0]["lifecycle"] = "DISABLED"
    redigest(other)
    with pytest.raises(ValueError, match="differ"):
        exact_snapshot(other, snapshot(target(), platform))


def test_revoked_operator_blocks_unpublished_direct_and_workflow_results():
    catalog = json.loads(json.dumps(build_catalog()))
    catalog["operators"][0]["lifecycle"] = "REVOKED"
    redigest(catalog)

    class Actions:
        request = {"kind": "OPERATOR", "target": target()}

        def get(self, action_id):
            assert action_id == "action"
            return {"request": self.request}

    class UnitOfWork:
        actions = Actions()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Store:
        uow = UnitOfWork()

        def transaction(self):
            return self.uow

    science = Science(Store(), None, None, operator_catalog=catalog)
    assert science.permit_publication("action") == {"decision": "DENY"}
    Store.uow.actions.request = {"kind": "WORKFLOW", "leaf_targets": [target()]}
    assert science.permit_publication("action") == {"decision": "DENY"}


def test_new_agent_catalog_requires_explicit_operator_version():
    legacy = tool_spec(build_tool_catalog("science"), "execution.submit_operator", "1.0.0")
    current = tool_spec(build_tool_catalog("p7-science"), "execution.submit_operator", "2.0.0")
    assert legacy["arguments"]["required"] == ["operator_id"]
    assert current["arguments"]["required"] == ["operator_id", "version_id"]


def test_new_agent_run_freezes_p7_tool_and_operator_catalogues():
    class Agents:
        def add_run(self, record):
            self.run = record

        def add_outbox(self, record):
            self.outbox = record

    class Uow:
        agents = Agents()

        @staticmethod
        def new_id():
            return "agent-run"

        @staticmethod
        def now():
            return datetime.now(UTC)

    tools = build_tool_catalog("p7-science")
    catalog = build_catalog()
    created = create_agent_run(
        Uow(),
        SimpleNamespace(project_id="project", task_id="task", spec_revision=1, control_version=1),
        Identity("principal", None),
        profile(),
        tool_catalog=tools,
        operator_catalog=catalog,
    )
    assert created["runtime_profile"]["tool_catalog_id"] == "p7-science-tools-v2"
    assert created["runtime_profile"]["tool_catalog_digest"] == tools["catalog_digest"]
    assert created["runtime_profile"]["operator_catalog_digest"] == catalog["catalog_digest"]
    assert (
        created["runtime_profile_digest"] == created["runtime_profile"]["deployment_profile_digest"]
    )


def test_new_agent_run_rejects_legacy_tool_catalogue():
    class Agents:
        @staticmethod
        def add_run(record):
            raise AssertionError("invalid run must not be saved")

    class Uow:
        agents = Agents()

        @staticmethod
        def new_id():
            return "agent-run"

        @staticmethod
        def now():
            return datetime.now(UTC)

    with pytest.raises(RuleViolation, match="P7 tool"):
        create_agent_run(
            Uow(),
            SimpleNamespace(
                project_id="project", task_id="task", spec_revision=1, control_version=1
            ),
            Identity("principal", None),
            profile(),
            tool_catalog=build_tool_catalog("science"),
            operator_catalog=build_catalog(),
        )


def test_agent_worker_rejects_a_run_frozen_to_other_catalogues():
    class Coordinator:
        @staticmethod
        def claim(owner, lease_seconds):
            return SimpleNamespace(
                runtime_profile={
                    "tool_catalog_digest": "sha256:" + "a" * 64,
                    "operator_catalog_digest": "sha256:" + "b" * 64,
                }
            )

    worker = KernelWorker(
        None,
        None,
        "worker",
        catalog_binding={
            "tool_catalog_id": "p7-science-tools-v2",
            "tool_catalog_digest": "sha256:" + "c" * 64,
            "operator_catalog_digest": "sha256:" + "b" * 64,
        },
    )
    worker.coordinator = Coordinator()
    with pytest.raises(RuntimeError, match="catalogue binding differs"):
        worker.process_one()


def test_agent_action_backend_uses_injected_operator_catalogue():
    catalog = json.loads(json.dumps(build_catalog()))
    catalog["operators"][0]["lifecycle"] = "DISABLED"
    redigest(catalog)
    backend = PlatformActionTools(object(), data=None, execution=None, operator_catalog=catalog)
    with pytest.raises(RuleViolation, match="unavailable"):
        backend._p3_operator("timeseries-completeness")


def test_remote_implementation_does_not_claim_a_local_image():
    value = operators(build_catalog())[3]
    remote = {
        "implementation_id": "remote-summary-v1",
        "binding_kind": "REMOTE",
        "runtime": "REMOTE",
        "implementation_digest": "sha256:" + "a" * 64,
        "protocol": "V7_REMOTE_HTTPS_V2",
        "resource_profile": "remote-bound-v1",
        "service_binding_id": "binding",
        "binding_revision": 1,
    }
    view = OperatorView.model_validate(value | {"implementations": [remote]})
    assert not hasattr(view.implementations[0], "image_digest")
    with pytest.raises(ValidationError):
        OperatorView.model_validate(
            value | {"implementations": [remote | {"image_digest": "sha256:" + "b" * 64}]}
        )


def test_production_release_requires_pinned_untampered_catalogues(tmp_path, monkeypatch):
    images = {
        "OPERATOR": "sha256:" + "a" * 64,
        "QGIS": "sha256:" + "b" * 64,
        "SPATIAL_NATIVE": "sha256:" + "c" * 64,
    }
    operator_path = tmp_path / "operators.json"
    catalog = write_catalog(operator_path, images)
    tool_path = tmp_path / "tools.json"
    tools = write_tool_catalog(tool_path, "p7-science")
    unsigned = {
        "schema_version": 2,
        "version": "0.10.0",
        "operator_catalog": {
            "digest": catalog["catalog_digest"],
            "file_sha256": file_digest(operator_path),
        },
        "tool_catalog": {
            "catalog_id": tools["catalog_id"],
            "digest": tools["catalog_digest"],
            "file_sha256": file_digest(tool_path),
        },
        "images": {"operator": images["OPERATOR"]},
        "wheels": wheel_evidence("0.10.0"),
    }
    lock = unsigned | {"release_digest": release_digest(unsigned)}
    lock_path = tmp_path / "release.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setenv("V7_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("V7_RELEASE_LOCK", str(lock_path))
    monkeypatch.setenv("V7_RELEASE_DIGEST", lock["release_digest"])
    checked = production_release(
        operator_path=operator_path,
        operator_catalog=catalog,
        tool_path=tool_path,
        tool_catalog=tools,
        images={"operator": images["OPERATOR"]},
        version="0.10.0",
    )
    assert checked["release_digest"] == lock["release_digest"]
    monkeypatch.setenv("V7_RELEASE_DIGEST", "sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="pinned"):
        load_release_lock(lock_path, "sha256:" + "0" * 64)
    monkeypatch.setenv("V7_RELEASE_DIGEST", lock["release_digest"])
    operator_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="file differs"):
        production_release(
            operator_path=operator_path,
            operator_catalog=catalog,
            tool_path=tool_path,
            tool_catalog=tools,
            version="0.10.0",
        )


def test_production_mode_rejects_implicit_source_catalog(monkeypatch):
    from platform_core.application.catalog import configured_catalog

    monkeypatch.setenv("V7_DEPLOYMENT_MODE", "production")
    monkeypatch.delenv("V7_OPERATOR_CATALOG", raising=False)
    with pytest.raises(ValueError, match="V7_OPERATOR_CATALOG"):
        configured_catalog()


def test_platform_and_execution_start_from_the_same_pinned_release(tmp_path, monkeypatch):
    from platform_api.app import create_app as create_platform
    from platform_execution.api import create_app as create_execution

    images = {
        "operator": "sha256:" + "a" * 64,
        "qgis": "sha256:" + "b" * 64,
        "spatial_native": "sha256:" + "c" * 64,
        "checker": "sha256:" + "d" * 64,
        "gdal_checker": "sha256:" + "e" * 64,
        "tabular": "sha256:" + "f" * 64,
        "cwl": "sha256:" + "9" * 64,
    }
    roles = {"CHECKER": images["checker"], "GDAL_CHECKER": images["gdal_checker"]}
    operator_path = tmp_path / "operators.json"
    catalog = write_catalog(
        operator_path,
        {
            "OPERATOR": images["operator"],
            "QGIS": images["qgis"],
            "SPATIAL_NATIVE": images["spatial_native"],
            "TABULAR": images["tabular"],
        },
    )
    tool_path = tmp_path / "tools.json"
    tools = write_tool_catalog(tool_path, "p7-science")
    checker_path = tmp_path / "checkers.json"
    checkers = build_checker_catalog(roles)
    checker_path.write_text(json.dumps({"images": roles, "catalog": checkers}), encoding="utf-8")
    unsigned = {
        "schema_version": 2,
        "version": version("groundwater-platform-api"),
        "operator_catalog": {
            "digest": catalog["catalog_digest"],
            "file_sha256": file_digest(operator_path),
        },
        "checker_catalog": {
            "digest": checkers["catalog_digest"],
            "file_sha256": file_digest(checker_path),
        },
        "tool_catalog": {
            "catalog_id": tools["catalog_id"],
            "digest": tools["catalog_digest"],
            "file_sha256": file_digest(tool_path),
        },
        "images": images,
        "wheels": wheel_evidence(version("groundwater-platform-api")),
        "web_assets": studio_evidence(),
    }
    release = unsigned | {"release_digest": release_digest(unsigned)}
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(release), encoding="utf-8")
    environment = {
        "V7_DEPLOYMENT_MODE": "production",
        "V7_RELEASE_LOCK": str(release_path),
        "V7_RELEASE_DIGEST": release["release_digest"],
        "V7_OPERATOR_CATALOG": str(operator_path),
        "V7_AGENT_TOOL_CATALOG": str(tool_path),
        "V7_CHECKER_CATALOG": str(checker_path),
        "V7_OPERATOR_IMAGE": images["operator"],
        "V7_SPATIAL_QGIS_IMAGE_ID": images["qgis"],
        "V7_SPATIAL_NATIVE_IMAGE_ID": images["spatial_native"],
        "V7_CHECKER_IMAGE": images["checker"],
        "V7_GDAL_CHECKER_IMAGE": images["gdal_checker"],
        "V7_TABULAR_OPERATOR_IMAGE": images["tabular"],
        "V7_CWL_IMAGE": images["cwl"],
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    class Database:
        @staticmethod
        def check_schema():
            return None

    with TestClient(create_execution(Database(), token="x" * 32)) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["release_digest"] == release["release_digest"]

    class Execution:
        @staticmethod
        def json(method, suffix):
            assert (method, suffix) == ("GET", "/readyz")
            return {
                "status": "ready",
                "operator_catalog_digest": catalog["catalog_digest"],
                "release_digest": release["release_digest"],
            }

    app = create_platform(Database())
    app.state.execution = Execution()
    with TestClient(app) as client:
        assert client.get("/api/v1/ready").status_code == 200


def test_execution_readiness_reports_loaded_catalog(tmp_path, monkeypatch):
    from platform_execution.api import create_app

    path = tmp_path / "operators.json"
    catalog = write_catalog(path)
    monkeypatch.setenv("V7_OPERATOR_CATALOG", str(path))

    class Database:
        def check_schema(self):
            return None

    with TestClient(create_app(Database(), token="x" * 32)) as client:
        assert client.get("/readyz").json()["operator_catalog_digest"] == catalog["catalog_digest"]


def test_platform_readiness_rejects_execution_catalog_mismatch(tmp_path, monkeypatch):
    from platform_api.app import create_app

    path = tmp_path / "operators.json"
    write_catalog(path)
    monkeypatch.setenv("V7_OPERATOR_CATALOG", str(path))

    class Database:
        def check_schema(self):
            return None

    class Execution:
        def json(self, method, suffix):
            assert (method, suffix) == ("GET", "/readyz")
            return {"status": "ready", "operator_catalog_digest": "sha256:" + "0" * 64}

    app = create_app(Database())
    app.state.execution = Execution()
    with TestClient(app) as client:
        assert client.get("/api/v1/ready").status_code == 503
