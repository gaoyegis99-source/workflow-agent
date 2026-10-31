from __future__ import annotations

import json
from pathlib import Path

import pytest
from platform_agent_runtime.composition import AgentWorkerComposition, AuthoringComposition
from platform_api.composition import PlatformComposition
from platform_core.application.catalog import write_catalog as write_operator_catalog
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.model_catalog import build_catalog
from platform_core.domain.agent_runtime import canonical_digest
from platform_data.composition import DataComposition
from platform_execution.composition import ExecutionAdminComposition, ExecutionComposition
from platform_infrastructure.composition import PlatformWorkerComposition
from platform_knowledge.composition import KnowledgeAdminComposition, KnowledgeComposition

DIGEST = "sha256:" + "a" * 64
TOKEN = "t" * 32


def _model_catalog(path: Path) -> None:
    unsigned = {
        "profile_id": "test-model",
        "provider": "DETERMINISTIC_FAKE",
        "model": "test-model",
        "request_mode": "RESPONSES",
        "supports_structured_output": True,
        "supports_query_by_response_id": False,
        "data_policy": "LOCAL_TEST_ONLY",
        "max_context_tokens": 1024,
        "max_output_tokens": 128,
        "max_reasoning_tokens": 0,
        "timeout_seconds": 5,
        "pricing_revision": "test-v1",
        "pricing": {
            "input_microunits_per_million": 0,
            "output_microunits_per_million": 0,
            "reasoning_microunits_per_million": 0,
        },
        "credential_env": "V7_TEST_MODEL_SECRET",
        "base_url": "http://127.0.0.1:9999/v1",
    }
    profile = unsigned | {"profile_digest": canonical_digest(unsigned)}
    path.write_text(json.dumps(build_catalog([profile], profile["profile_id"])))


def _values(tmp_path: Path) -> dict[str, str]:
    model_catalog = tmp_path / "model-catalog.json"
    _model_catalog(model_catalog)
    operator_catalog = tmp_path / "operator-catalog.json"
    images = {
        "OPERATOR": DIGEST,
        "QGIS": DIGEST,
        "SPATIAL_NATIVE": DIGEST,
        "TABULAR": DIGEST,
    }
    write_operator_catalog(operator_catalog, images)
    checker_catalog = tmp_path / "checker-catalog.json"
    checker_images = {"CHECKER": DIGEST, "GDAL_CHECKER": DIGEST}
    checker_catalog.write_text(
        json.dumps({"images": checker_images, "catalog": build_checker_catalog(checker_images)}),
        encoding="utf-8",
    )
    return {
        "V7_DATABASE_URL": "postgresql://platform.test/v7",
        "V7_EXECUTION_DATABASE_URL": "postgresql://execution.test/v7",
        "V7_AGENT_CHECKPOINT_DATABASE_URL": "postgresql://checkpoint.test/v7",
        "V7_AGENT_RUNTIME_PROFILE": str(tmp_path / "runtime-profile.json"),
        "V7_MODEL_CATALOG": str(model_catalog),
        "V7_AGENT_TOOL_CATALOG": str(tmp_path / "tool-catalog.json"),
        "V7_EXECUTION_WORKDIR": str(tmp_path / "work"),
        "V7_DATA_DATABASE_URL": "postgresql://data.test/v7",
        "V7_KNOWLEDGE_DATABASE_URL": "postgresql://knowledge.test/v7",
        "V7_DATA_STORAGE": str(tmp_path / "data-storage"),
        "V7_KNOWLEDGE_STORAGE": str(tmp_path / "knowledge-storage"),
        "V7_DATA_URL": "https://data.example.test",
        "V7_KNOWLEDGE_URL": "https://knowledge.example.test",
        "V7_EXECUTION_URL": "https://execution.example.test",
        "V7_PLATFORM_URL": "https://platform.example.test",
        "V7_PLATFORM_DATA_TOKEN": TOKEN,
        "V7_PLATFORM_KNOWLEDGE_TOKEN": TOKEN,
        "V7_PLATFORM_EXECUTION_TOKEN": TOKEN,
        "V7_EXECUTION_DATA_TOKEN": TOKEN,
        "V7_KNOWLEDGE_DATA_TOKEN": TOKEN,
        "V7_EXECUTION_PLATFORM_TOKEN": TOKEN,
        "V7_OPERATOR_CATALOG": str(operator_catalog),
        "V7_CHECKER_CATALOG": str(checker_catalog),
        "V7_OPERATOR_IMAGE": DIGEST,
        "V7_CHECKER_IMAGE": DIGEST,
        "V7_GDAL_CHECKER_IMAGE": DIGEST,
        "V7_SPATIAL_QGIS_IMAGE_ID": DIGEST,
        "V7_SPATIAL_NATIVE_IMAGE_ID": DIGEST,
        "V7_TABULAR_OPERATOR_IMAGE": DIGEST,
        "V7_CWL_IMAGE": DIGEST,
        "V7_EXECUTION_CALLBACK_URL": "https://workflow.example.test/callback",
        "V7_WORKER_HEARTBEAT_FILE": str(tmp_path / "worker.heartbeat"),
    }


def test_executable_compositions_build_consistent_explicit_contexts(tmp_path):
    values = _values(tmp_path)

    platform = PlatformComposition.from_environment(values)
    execution = ExecutionComposition.from_environment(values)
    agent = AgentWorkerComposition.from_environment(values)
    authoring = AuthoringComposition.from_environment(values)
    data = DataComposition.from_environment(values)
    knowledge = KnowledgeComposition.from_environment(values)
    platform_worker = PlatformWorkerComposition.from_environment(values)

    assert (
        platform.deployment
        == execution.runtime.deployment
        == agent.deployment
        == data.deployment
        == knowledge.deployment
        == platform_worker.deployment
    )
    assert platform.catalog.operator_catalog_path == execution.catalog.operator_catalog_path
    assert execution.runtime.workflow_engine_callback_url == values["V7_EXECUTION_CALLBACK_URL"]
    assert agent.model_profile["profile_id"] == "test-model"
    assert agent.model_provider_secret is None
    assert platform.operator_catalog["catalog_digest"] == agent.operator_catalog["catalog_digest"]
    assert (
        platform.operator_catalog["catalog_digest"] == authoring.operator_catalog["catalog_digest"]
    )
    assert platform.checker_catalog["catalog_digest"] == agent.checker_catalog["catalog_digest"]
    heartbeat = tmp_path / "worker.heartbeat"
    assert execution.runtime.worker_heartbeat_file == heartbeat
    assert agent.heartbeat_file == data.heartbeat_file == knowledge.heartbeat_file == heartbeat
    assert platform_worker.heartbeat_file == heartbeat


@pytest.mark.parametrize(
    ("name", "composition", "error"),
    [
        (
            "V7_OPERATOR_CATALOG",
            PlatformComposition.from_environment,
            "V7_OPERATOR_CATALOG_REQUIRED",
        ),
        ("V7_OPERATOR_CATALOG", AgentWorkerComposition.from_environment, "V7_OPERATOR_CATALOG"),
        ("V7_OPERATOR_CATALOG", AuthoringComposition.from_environment, "V7_OPERATOR_CATALOG"),
        ("V7_CHECKER_CATALOG", PlatformComposition.from_environment, "V7_CHECKER_CATALOG_REQUIRED"),
        ("V7_CHECKER_CATALOG", AgentWorkerComposition.from_environment, "V7_CHECKER_CATALOG"),
    ],
)
def test_catalog_consumers_fail_closed_without_explicit_pinned_inputs(
    tmp_path, name, composition, error
):
    values = _values(tmp_path)
    values.pop(name)

    with pytest.raises((RuntimeError, ValueError), match=error):
        composition(values)


def test_execution_composition_rejects_invalid_callback_before_application_boot(tmp_path):
    values = _values(tmp_path)
    values["V7_EXECUTION_CALLBACK_URL"] = "file:///not-a-service"

    with pytest.raises(ValueError, match="V7_EXECUTION_CALLBACK_URL_INVALID"):
        ExecutionComposition.from_environment(values)


def test_data_worker_compositions_do_not_require_api_credentials_or_spatial_inputs(tmp_path):
    values = _values(tmp_path)
    for key in (
        "V7_PLATFORM_DATA_TOKEN",
        "V7_EXECUTION_DATA_TOKEN",
        "V7_KNOWLEDGE_DATA_TOKEN",
        "V7_SPATIAL_QGIS_IMAGE_ID",
        "V7_DATA_SPATIAL_WORK_ROOT",
    ):
        values.pop(key, None)

    worker = DataComposition.from_environment(values, role="worker")
    document = DataComposition.from_environment(values, role="document")
    database = DataComposition.from_environment(values, role="database")

    assert worker.credentials == document.credentials == database.credentials == {}
    assert worker.spatial_image is None
    assert document.spatial_work_root is None
    with pytest.raises(ValueError, match="V7_SPATIAL_QGIS_IMAGE_ID_REQUIRED"):
        DataComposition.from_environment(values, role="spatial")


def test_database_admin_compositions_only_require_their_owned_database_url(tmp_path):
    values = _values(tmp_path)
    assert (
        ExecutionAdminComposition.from_environment(
            {"V7_EXECUTION_DATABASE_URL": values["V7_EXECUTION_DATABASE_URL"]}
        ).database_url
        == values["V7_EXECUTION_DATABASE_URL"]
    )
    assert (
        KnowledgeAdminComposition.from_environment(
            {"V7_KNOWLEDGE_DATABASE_URL": values["V7_KNOWLEDGE_DATABASE_URL"]}
        ).database_url
        == values["V7_KNOWLEDGE_DATABASE_URL"]
    )


@pytest.mark.parametrize("lane", ["action", "acceptance", "knowledge"])
def test_nonpackage_platform_worker_lanes_do_not_require_operator_catalog(tmp_path, lane):
    values = _values(tmp_path)
    values.pop("V7_OPERATOR_CATALOG")

    composition = PlatformWorkerComposition.from_environment(values, lane=lane)

    assert composition.operator_catalog is None


def test_package_platform_worker_lane_requires_operator_catalog(tmp_path):
    values = _values(tmp_path)
    values.pop("V7_OPERATOR_CATALOG")

    with pytest.raises(RuntimeError, match="V7_OPERATOR_CATALOG"):
        PlatformWorkerComposition.from_environment(values, lane="package")


def test_platform_worker_composition_rejects_unknown_lane(tmp_path):
    with pytest.raises(ValueError, match="Unknown Platform worker lane"):
        PlatformWorkerComposition.from_environment(_values(tmp_path), lane="unknown")
