from fastapi.testclient import TestClient
from platform_agent_runtime.profiles import profile as runtime_profile
from platform_api.app import create_app
from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.domain.agent_runtime import DecisionProposal


def test_agent_projection_and_idempotent_answer_are_public_without_checkpoint(model_agent_setup):
    setup = model_agent_setup
    coordinator = AgentCoordinator(setup["database"])
    claim = coordinator.claim("question-worker", 30)
    frame = coordinator.prepare_frame(claim)
    coordinator.accept_proposal(
        claim,
        frame,
        DecisionProposal(
            "CLARIFICATION_REQUEST",
            frame.frame_id,
            frame.task_spec_revision,
            {
                "gap_id": "unit-gap",
                "question": "Which unit applies?",
                "why_needed": "The unit is required for interpretation.",
                "answer_schema": {"type": "object"},
                "blocked_actions": [],
            },
        ),
    )
    headers = {"Authorization": "Bearer " + setup["token"]}
    path = f"/api/v1/projects/{setup['project_id']}/tasks/{setup['task']['task_id']}"
    with TestClient(
        create_app(
            setup["database"],
            agent_runtime_profile=runtime_profile(),
            agent_model_catalog=setup["catalog"],
        )
    ) as client:
        current = client.get(path + "/agent", headers=headers)
        assert current.status_code == 200, current.text
        body = current.json()
        assert body["status"] == "QUESTION"
        assert body["questions"][0]["question"]["question"] == "Which unit applies?"
        assert "frames" not in body and "runtime_ref" not in body
        question_id = body["questions"][0]["question_id"]
        answer_headers = headers | {"Idempotency-Key": "answer-unit"}
        first = client.post(
            path + f"/questions/{question_id}/answers",
            headers=answer_headers,
            json={"expected_spec_revision": 1, "answer": {"unit": "metre"}},
        )
        second = client.post(
            path + f"/questions/{question_id}/answers",
            headers=answer_headers,
            json={"expected_spec_revision": 1, "answer": {"unit": "metre"}},
        )
        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        schema = client.get("/api/v1/openapi.json").json()
        assert (
            path.replace(setup["project_id"], "{project_id}").replace(
                setup["task"]["task_id"], "{task_id}"
            )
            + "/agent"
            in schema["paths"]
        )
