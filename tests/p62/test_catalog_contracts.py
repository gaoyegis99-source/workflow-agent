import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from platform_agent_runtime.profiles import profile as runtime_profile
from platform_contracts.control import TaskCreate
from platform_core.application.model_catalog import build_catalog, load_catalog
from platform_core.application.runtime_config import load_runtime_profile
from platform_core.domain.control import RuleViolation
from pydantic import ValidationError

from tests.p62.conftest import model_budget, model_profile


def test_catalog_is_deterministic_and_platform_worker_load_same_digest(tmp_path):
    selected = model_profile()
    catalog = build_catalog([selected], selected["profile_id"])
    path = tmp_path / "model-catalog.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    assert load_catalog(path) == catalog
    assert load_catalog(path)["catalog_digest"] == catalog["catalog_digest"]


def test_runtime_profile_file_is_identical_for_platform_and_worker(tmp_path):
    from platform_agent_runtime.profiles import load_deployment_profile

    selected = runtime_profile()
    path = tmp_path / "agent-runtime-profile.json"
    path.write_text(json.dumps(selected), encoding="utf-8")
    assert load_runtime_profile(path) == selected
    assert load_deployment_profile(path) == selected

    tampered = selected | {"max_model_calls_per_advance": 2}
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RuleViolation):
        load_runtime_profile(path)
    with pytest.raises(ValueError):
        load_deployment_profile(path)


def test_catalog_tampering_and_arbitrary_endpoint_are_rejected(tmp_path):
    selected = model_profile()
    catalog = build_catalog([selected], selected["profile_id"])
    tampered = copy.deepcopy(catalog)
    tampered["profiles"][0]["model"] = "floating-model"
    path = tmp_path / "bad-catalog.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RuleViolation):
        load_catalog(path)
    selected["base_url"] = "http://example.test/v1"
    selected["profile_digest"] = "sha256:" + "0" * 64
    with pytest.raises(RuleViolation):
        build_catalog([selected], selected["profile_id"])


def test_agent_task_requires_model_budget_when_catalog_is_enabled(database, owner):
    actor, _ = owner
    selected = model_profile()
    catalog = build_catalog([selected], selected["profile_id"])
    from platform_core.application.control import ControlService

    control = ControlService(
        database,
        agent_runtime_profile=runtime_profile(),
        agent_model_catalog=catalog,
    )
    project = control.create_project(actor, "p62-budget-project", "budget")["project_id"]
    with pytest.raises(RuleViolation) as error:
        control.create_task(
            actor,
            project,
            "missing-model-budget",
            {
                "execution_mode": "AGENT",
                "spec": {"goal": "missing budget", "requirements": []},
                "budget": {"execution_count": 1, "max_in_flight": 1},
            },
        )
    assert error.value.code == "MALFORMED_REQUEST"


def test_model_budget_contract_is_strict():
    value = {
        "execution_mode": "AGENT",
        "spec": {"goal": "strict budget", "requirements": []},
        "budget": {"execution_count": 1, "max_in_flight": 1},
        "model_budget": model_budget(),
    }
    assert TaskCreate.model_validate(value).model_budget.max_model_calls == 4
    value["model_budget"]["unknown"] = 1
    with pytest.raises(ValidationError):
        TaskCreate.model_validate(value)


def test_read_only_model_usage_api(model_agent_setup):
    from platform_api.app import create_app

    setup = model_agent_setup
    headers = {"Authorization": "Bearer " + setup["token"]}
    with TestClient(
        create_app(
            setup["database"],
            agent_runtime_profile=runtime_profile(),
            agent_model_catalog=setup["catalog"],
        )
    ) as client:
        response = client.get(
            f"/api/v1/projects/{setup['project_id']}/tasks/{setup['task']['task_id']}/model-usage",
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert response.json()["used_calls"] == response.json()["reserved_calls"] == 0


def test_environment_configuration_enables_agent_only_as_a_complete_pair(
    database, tmp_path, monkeypatch
):
    from platform_api.app import create_app

    profile_path = tmp_path / "runtime.json"
    profile_path.write_text(json.dumps(runtime_profile()), encoding="utf-8")
    monkeypatch.setenv("V7_AGENT_RUNTIME_PROFILE", os.fspath(profile_path))
    with TestClient(create_app(database)) as client:
        assert client.get("/api/v1/ready").status_code == 503

    catalog = build_catalog([model_profile()], "approved-model-v1")
    catalog_path = tmp_path / "models.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setenv("V7_MODEL_CATALOG", os.fspath(catalog_path))
    with TestClient(create_app(database)) as client:
        assert client.get("/api/v1/ready").status_code == 200


def test_deployment_generators_write_secret_free_valid_catalogues(tmp_path):
    unsigned = model_profile()
    unsigned.pop("profile_digest")
    source = tmp_path / "profiles.json"
    source.write_text(json.dumps([unsigned]), encoding="utf-8")
    catalog_path = tmp_path / "model-catalog.json"
    runtime_path = tmp_path / "runtime-profile.json"
    embedding_path = "deploy/p8/embedding-catalog.disabled.json"
    subprocess.run(
        [
            sys.executable,
            "scripts/generate_model_catalog.py",
            "--profiles",
            os.fspath(source),
            "--default-profile",
            "approved-model-v1",
            "--output",
            os.fspath(catalog_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            "scripts/generate_agent_runtime_profile.py",
            "--embedding-catalog",
            embedding_path,
            "--output",
            os.fspath(runtime_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert load_catalog(catalog_path)["default_profile_id"] == "approved-model-v1"
    embedding = json.loads(Path(embedding_path).read_text(encoding="utf-8"))
    assert load_runtime_profile(runtime_path) == runtime_profile() | {
        "embedding_catalog_digest": embedding["catalog_digest"]
    }
    assert "API_KEY" in catalog_path.read_text()
    assert "secret" not in catalog_path.read_text().lower()
