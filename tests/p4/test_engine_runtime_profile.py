import copy
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from platform_contracts.engine_runtime import (
    OFF_PROFILE_ID,
    ON_PROFILE_ID,
    diagnostic,
    profile,
    validate,
)
from platform_execution.adapters.repository import Repository
from platform_execution.api import create_app
from platform_execution.application.workflow_runner import WorkflowRunner

from tests.p4.test_execution_plan import execution_request

ROOT = Path(__file__).resolve().parents[2]
ENGINE_SPEC = importlib.util.spec_from_file_location(
    "p4_r5_cwl_engine", ROOT / "runtimes/cwl/engine.py"
)
assert ENGINE_SPEC is not None and ENGINE_SPEC.loader is not None
ENGINE = importlib.util.module_from_spec(ENGINE_SPEC)
ENGINE_SPEC.loader.exec_module(ENGINE)


def test_profiles_are_exact_allowlisted_and_differ_only_by_chaining():
    disabled = profile(OFF_PROFILE_ID)
    enabled = profile(ON_PROFILE_ID)
    assert disabled["disable_chaining"] is True
    assert enabled["disable_chaining"] is False
    assert disabled["profile_digest"] != enabled["profile_digest"]
    comparable = set(disabled) - {"profile_id", "profile_digest", "disable_chaining"}
    assert {key: disabled[key] for key in comparable} == {key: enabled[key] for key in comparable}
    assert diagnostic(enabled) == {
        "profile_id": ON_PROFILE_ID,
        "profile_digest": enabled["profile_digest"],
        "chaining_enabled": True,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(max_cores=3),
        lambda value: value.update(profile_digest="sha256:" + "0" * 64),
        lambda value: value.update(extra=True),
        lambda value: value.pop("retry_count"),
    ],
)
def test_profile_tampering_is_rejected(mutate):
    value = profile()
    mutate(value)
    with pytest.raises(ValueError):
        validate(value)


def test_first_acceptance_freezes_profile_and_replay_does_not_replace_it(service_databases):
    database = service_databases[1]
    action = "profile_freeze"
    body = execution_request(action)
    token = "platform-" + "x" * 32
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(database, token, OFF_PROFILE_ID)) as disabled:
        first = disabled.post("/internal/actions/" + action, json=body, headers=headers)
    with TestClient(create_app(database, token, ON_PROFILE_ID)) as enabled:
        replay = enabled.post("/internal/actions/" + action, json=body, headers=headers)
    assert first.status_code == replay.status_code == 202
    row = Repository(database).get(action=action)
    assert row["runtime"]["_engine_runtime_profile"] == profile(OFF_PROFILE_ID)
    assert first.json() == replay.json()


def test_public_view_exposes_diagnostic_but_not_internal_profile(service_databases):
    database = service_databases[1]
    action = "profile_view"
    body = execution_request(action)
    token = "platform-" + "x" * 32
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(database, token, ON_PROFILE_ID)) as client:
        submitted = client.post("/internal/actions/" + action, json=body, headers=headers)
        assert submitted.status_code == 202
        execution = Repository.identity(action)
        response = client.get(
            "/internal/projects/project/executions/" + execution,
            headers=headers,
        )
    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert "_engine_runtime_profile" not in runtime
    assert runtime["engine_profile"] == diagnostic(profile(ON_PROFILE_ID))


def test_profile_copies_cannot_mutate_registry():
    value = profile()
    changed = copy.deepcopy(value)
    changed["max_jobs"] = 999
    assert profile() == value
    assert json.dumps(profile(), sort_keys=True) == json.dumps(value, sort_keys=True)


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    [(OFF_PROFILE_ID, "true"), (ON_PROFILE_ID, "false")],
)
def test_engine_builds_only_the_profile_chaining_argument(profile_id, expected):
    command = ENGINE.toil_command(
        {
            "compiled": {"entrypoint": "workflow.cwl"},
            "engine_runtime_profile": profile(profile_id),
            "restart": False,
        }
    )
    assert command[command.index("--disableChaining") + 1] == expected
    assert command[command.index("--retryCount") + 1] == "0"
    assert command[command.index("--maxCores") + 1] == "2"


def test_client_cannot_supply_engine_profile(service_databases):
    database = service_databases[1]
    action = "client_profile"
    body = execution_request(action) | {"engine_runtime_profile": profile(ON_PROFILE_ID)}
    token = "platform-" + "x" * 32
    with TestClient(create_app(database, token)) as client:
        response = client.post(
            "/internal/actions/" + action,
            json=body,
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_BINDING"


def test_legacy_profileless_workflow_is_reported_and_frozen_as_off(
    service_databases, tmp_path, monkeypatch
):
    database = service_databases[1]
    action = "legacy_profileless"
    body = execution_request(action)
    repository = Repository(database)
    stored = repository.create(action, body)
    assert "_engine_runtime_profile" not in stored["runtime"]

    token = "platform-" + "x" * 32
    with TestClient(create_app(database, token, ON_PROFILE_ID)) as client:
        response = client.get(
            "/internal/projects/project/executions/" + Repository.identity(action),
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 200
    assert response.json()["runtime"]["engine_profile"] == diagnostic(profile(OFF_PROFILE_ID)) | {
        "compatibility": "0.3.7-unlabeled"
    }
    assert WorkflowRunner.engine_profile(stored) == profile(OFF_PROFILE_ID)

    class ClaimRepository:
        @staticmethod
        def change(row, **changes):
            row.update(changes)
            return True

    runner = WorkflowRunner(ClaimRepository(), object(), object(), object(), "http://execution")
    monkeypatch.setattr(runner, "stage", lambda row, root: (row, root))
    result, root = runner.step(stored, tmp_path)
    assert root == tmp_path
    assert result["runtime"]["_engine_runtime_profile"] == profile(OFF_PROFILE_ID)
    assert result["runtime"]["_engine_profile_legacy_unlabeled"] is True
