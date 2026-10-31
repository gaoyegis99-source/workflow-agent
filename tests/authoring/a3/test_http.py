from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_api.composition import PlatformComposition
from platform_core.application.catalog import build_catalog
from platform_core.application.control import ControlService
from platform_core.application.workflows import PackageProcessor

from tests.authoring.a2.helpers import profile_source
from tests.authoring.a3.test_candidate import Data, Validator, frozen_definition


def test_candidate_trial_and_release_routes_are_exact(database, owner):
    actor, token = owner
    values = frozen_definition(database, owner)
    _, project, definition, revision, validation, *_ = values
    composition = replace(PlatformComposition.for_test(), cwl_image=Validator.image_id)
    with TestClient(create_app(database, composition=composition)) as client:
        prefix = f"/api/v1/projects/{project}/workflow-definitions/{definition}"
        headers = {
            "Authorization": "Bearer " + token,
            "Idempotency-Key": "candidate-http",
        }
        response = client.post(
            prefix + "/candidates",
            headers=headers,
            json={
                "revision": revision["revision"],
                "source_digest": revision["source_digest"],
                "validation_id": validation["validation_id"],
            },
        )
        assert response.status_code == 202, response.text
        candidate_id = response.json()["candidate_id"]
        assert PackageProcessor(
            database,
            Validator(),
            operator_catalog=build_catalog(),
            owner="http-package",
        ).process_one()
        view = client.get(
            prefix + "/candidates/" + candidate_id,
            headers={"Authorization": "Bearer " + token},
        )
        assert view.status_code == 200
        assert view.json()["status"] == "READY"
        candidate = view.json()
        control = ControlService(database)
        task = control.create_task(
            actor,
            project,
            "http-task",
            {
                "execution_mode": "DIRECT",
                "spec": {"goal": "HTTP trial", "requirements": []},
                "budget": {"execution_count": 1, "max_in_flight": 1},
            },
        )
        asset = {
            "project_id": project,
            "asset_id": "asset",
            "version_id": "1",
            "version_manifest_digest": "sha256:" + "b" * 64,
        }
        selection = {
            "kind": "TABLE",
            "asset": asset,
            "representation_id": "normalized",
            "representation_digest": "sha256:" + "c" * 64,
        }
        target = {"kind": "WORKFLOW", "package_ref": candidate["package_ref"]}
        control.grant(
            actor,
            project,
            "http-grant",
            {
                "task_id": task["task_id"],
                "targets": [target, profile_source()["implementations"]["profile_impl"]],
                "resources": [asset],
                "derived_outputs": True,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        client.app.state.data = Data(selection)
        trial = client.post(
            prefix + "/trial-runs",
            headers={
                "Authorization": "Bearer " + token,
                "Idempotency-Key": "trial-http",
            },
            json={
                "candidate_id": candidate_id,
                "task_id": task["task_id"],
                "expected_spec_revision": 1,
                "inputs": {"table_input": selection},
                "max_node_executions": 1,
                "trial_expectations": [
                    {
                        "output": "report",
                        "schema_id": "table-profile-report-v1",
                    }
                ],
            },
        )
        assert trial.status_code == 202, trial.text
        assert trial.json()["command"]["action_id"]
        assert trial.json()["trial_case_digest"].startswith("sha256:")
        paths = client.get("/api/v1/openapi.json").json()["paths"]
        assert (
            "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/trial-runs" in paths
        )
        assert (
            "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/releases/"
            "{version_label}" in paths
        )
        assert not any(
            "workflow-imports" in path or "workflow-marketplace" in path for path in paths
        )
