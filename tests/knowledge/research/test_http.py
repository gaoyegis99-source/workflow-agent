import secrets

from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_core.application.control import ControlService

DIGEST = "sha256:" + "a" * 64


class RemoteFacts:
    def __init__(self, project):
        self.asset = {
            "project_id": project,
            "asset_id": "asset",
            "version_id": "1",
            "version_manifest_digest": DIGEST,
        }
        self.evidence = {
            "project_id": project,
            "document_id": "document",
            "version_id": "1",
            "unit_id": "unit",
            "content_digest": DIGEST,
            "start_char": 0,
            "end_char": 10,
            "exact_text_digest": DIGEST,
            "normalization": "utf8-nfc-lf-v1",
        }

    def json(self, method, path, **kwargs):
        if "/assets/" in path:
            return {"ref": self.asset, "manifest": {}, "semantic_revisions": []}
        if "/units/" in path:
            return {
                "evidence_ref": self.evidence,
                "text": "exact text",
                "prefix": "",
                "suffix": "",
                "lifecycle_revision": 1,
                "availability": "ACTIVE",
                "epistemic_status": "DECLARED",
                "review_state": "UNREVIEWED",
                "read_receipt_id": "receipt",
            }
        raise AssertionError((method, path, kwargs))


class WithdrawnFacts(RemoteFacts):
    def json(self, method, path, **kwargs):
        value = super().json(method, path, **kwargs)
        if "/units/" in path:
            value["availability"] = "WITHDRAWN"
            value["lifecycle_revision"] = 2
        return value


def test_real_public_research_routes_recheck_roles_and_return_current_state(
    database,  # noqa: F811
    owner,  # noqa: F811
):
    actor, token = owner
    control = ControlService(database)
    project = control.create_project(actor, "http-project", "HTTP")["project_id"]
    task = control.create_task(
        actor,
        project,
        "http-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "HTTP research", "requirements": []},
            "budget": {"execution_count": 2, "max_in_flight": 1},
        },
    )
    viewer_id = database.create_principal("viewer")
    viewer_token = database.issue_token(viewer_id)["token"]
    database.set_member(project, viewer_id, "VIEWER", actor=actor)
    facts = RemoteFacts(project)
    app = create_app(database)
    app.state.data = facts
    app.state.knowledge = facts
    prefix = f"/api/v1/projects/{project}/tasks/{task['task_id']}"
    analysis = {
        "expected_spec_revision": 1,
        "question": "What does the evidence support?",
        "research_objects": ["well-a"],
        "input_roles": [
            {
                "role": "input",
                "source": {"kind": "ASSET", "ref": facts.asset},
                "description": "Input asset",
            }
        ],
        "observed_sources": [{"kind": "EVIDENCE", "ref": facts.evidence}],
    }
    with TestClient(app) as client:
        owner_headers = {"Authorization": "Bearer " + token}
        viewer_headers = {"Authorization": "Bearer " + viewer_token}
        created = client.post(
            prefix + "/analysis-revisions",
            headers=owner_headers | {"Idempotency-Key": "analysis"},
            json=analysis,
        )
        assert created.status_code == 201, created.text
        denied = client.post(
            prefix + "/analysis-revisions",
            headers=viewer_headers | {"Idempotency-Key": secrets.token_hex(8)},
            json=analysis,
        )
        assert denied.status_code == 403
        registered = client.post(
            prefix + f"/analysis-revisions/{created.json()['revision_id']}/register",
            headers=owner_headers | {"Idempotency-Key": "register"},
            json={"expected_spec_revision": 1},
        )
        assert registered.status_code == 201, registered.text
        claim = client.post(
            prefix + "/claims",
            headers=owner_headers | {"Idempotency-Key": "claim"},
            json={
                "expected_spec_revision": 1,
                "analysis_registration_id": registered.json()["registration_id"],
                "proposition_type": "DESCRIPTIVE",
                "statement": "The source declares a datum.",
                "scope": {"objects": ["well-a"]},
                "limitations": ["Not independently corroborated"],
                "sources": [
                    {
                        "relation": "CITED",
                        "role": "basis",
                        "source": {"kind": "EVIDENCE", "ref": facts.evidence},
                    }
                ],
                "support_status": "UNVERIFIED",
            },
        )
        assert claim.status_code == 201, claim.text
        result = client.get(prefix + "/research", headers=viewer_headers)
        assert result.status_code == 200, result.text
        assert len(result.json()["analysis_revisions"]) == 1
        assert result.json()["analysis_revisions"][0]["status"] == "REGISTERED"
        assert len(result.json()["claims"]) == 1


def test_withdrawn_evidence_cannot_enter_a_new_research_revision(database, owner):  # noqa: F811
    actor, token = owner
    control = ControlService(database)
    project = control.create_project(actor, "withdrawn-project", "Withdrawn")["project_id"]
    task = control.create_task(
        actor,
        project,
        "withdrawn-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Do not use withdrawn evidence", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    facts = WithdrawnFacts(project)
    app = create_app(database)
    app.state.data = facts
    app.state.knowledge = facts
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/projects/{project}/tasks/{task['task_id']}/analysis-revisions",
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": "withdrawn"},
            json={
                "expected_spec_revision": 1,
                "question": "Can this withdrawn source be used?",
                "research_objects": ["source"],
                "observed_sources": [{"kind": "EVIDENCE", "ref": facts.evidence}],
            },
        )
    assert response.status_code == 409
    assert response.json()["code"] == "PRECONDITION_UNRESOLVED"
