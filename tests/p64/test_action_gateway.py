from platform_core.application.action_tools import ActionResult, ActionToolGateway
from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.tool_catalog import build_tool_catalog
from platform_core.domain.agent_runtime import DecisionProposal


class Backend:
    def __init__(self):
        self.calls = []

    def execute(self, tool_id, arguments, context, tool_request_id):
        self.calls.append((tool_id, arguments, context, tool_request_id))
        return ActionResult(
            {
                "command_id": "command-one",
                "action_id": "action-one",
                "status": "RECORDED",
            },
            operation="SUBMIT_OPERATOR",
            business_id="action-one",
        )


def proposal(frame):
    return DecisionProposal(
        "ACTION_REQUESTS",
        frame.frame_id,
        frame.task_spec_revision,
        {
            "requests": [
                {
                    "call_id": "submit-completeness",
                    "tool_id": "execution.submit_operator",
                    "tool_version": "1.0.0",
                    "purpose": "Run the required completeness calculation",
                    "arguments": {"operator_id": "timeseries-completeness"},
                    "expected_observation": "A stable command receipt",
                }
            ]
        },
    )


def test_action_catalog_enables_only_controlled_stage():
    read = build_tool_catalog()
    action = build_tool_catalog("actions")
    assert {item["operation_class"] for item in read["tools"]} == {"READ_ONLY"}
    assert {item["operation_class"] for item in action["tools"]} == {
        "READ_ONLY",
        "CONTROLLED_WRITE",
    }
    writes = [
        item["tool_id"] for item in action["tools"] if item["operation_class"] == "CONTROLLED_WRITE"
    ]
    assert writes == [
        "execution.submit_operator",
        "deliverable.register",
        "deliverable.register_exact",
        "acceptance.request",
    ]


def test_action_request_is_linked_once_and_replay_does_not_repeat(model_agent_setup):
    catalog = build_tool_catalog("actions")
    coordinator = AgentCoordinator(
        model_agent_setup["database"],
        context_compiler=ContextCompiler(catalog, max_context_tokens=20_000),
    )
    claim = coordinator.claim("p64-worker", 30)
    frame = coordinator.prepare_frame(claim)
    accepted = coordinator.accept_proposal(claim, frame, proposal(frame))
    backend = Backend()
    gateway = ActionToolGateway(model_agent_setup["database"], catalog, backend)

    first = gateway.execute_proposal(accepted["proposal_id"])
    second = gateway.execute_proposal(accepted["proposal_id"])
    assert first == second
    assert len(backend.calls) == 1

    with model_agent_setup["database"].transaction() as uow:
        links = uow.agents.action_links(model_agent_setup["agent_run_id"])
        requests = uow.agents.tool_requests(model_agent_setup["agent_run_id"])
        observations = uow.agents.tool_observations(model_agent_setup["agent_run_id"])
    assert len(links) == len(requests) == len(observations) == 1
    assert links[0]["business_id"] == "action-one"
    assert links[0]["request_digest"] == requests[0]["request_digest"]


def test_action_context_rejects_read_tool_in_action_proposal(model_agent_setup):
    catalog = build_tool_catalog("actions")
    coordinator = AgentCoordinator(
        model_agent_setup["database"],
        context_compiler=ContextCompiler(catalog, max_context_tokens=20_000),
    )
    claim = coordinator.claim("p64-worker-read", 30)
    frame = coordinator.prepare_frame(claim)
    value = proposal(frame)
    body = dict(value.body)
    body["requests"] = [dict(body["requests"][0], tool_id="task.read_current", arguments={})]
    accepted = coordinator.accept_proposal(
        claim,
        frame,
        DecisionProposal("ACTION_REQUESTS", frame.frame_id, frame.task_spec_revision, body),
    )
    gateway = ActionToolGateway(model_agent_setup["database"], catalog, Backend())
    import pytest
    from platform_core.domain.control import RuleViolation

    with pytest.raises(RuleViolation) as error:
        gateway.execute_proposal(accepted["proposal_id"])
    assert error.value.code == "UNSUPPORTED_TARGET"


class Data:
    def __init__(self, selection):
        self.selection = selection

    def json(self, method, path, **kwargs):
        assert method == "POST" and path.endswith("/resolve")
        assert kwargs["json"] == self.selection
        return {
            "selection": self.selection,
            "semantics": {
                "timezone": "UTC",
                "value_unit": "m",
                "quantity": "water_level_elevation",
            },
            "representation": {"digest": self.selection["representation_digest"]},
        }


class Execution:
    def json(self, *args, **kwargs):
        raise AssertionError("submission admission must not query Execution synchronously")


def test_production_action_uses_current_task_contract_and_agent_identity(model_agent_setup):
    from datetime import UTC, datetime, timedelta

    from platform_agent_runtime.action_tools import PlatformActionTools
    from platform_agent_runtime.profiles import profile
    from platform_core.application.catalog import OPERATORS, target
    from platform_core.application.control import ControlService
    from platform_core.application.model_catalog import build_catalog

    from tests.p62.conftest import model_budget, model_profile

    setup = model_agent_setup
    asset = {
        "project_id": setup["project_id"],
        "asset_id": "observations",
        "version_id": "v1",
        "version_manifest_digest": "sha256:" + "a" * 64,
    }
    selection = {
        "asset": asset,
        "representation_id": "normalized",
        "representation_digest": "sha256:" + "b" * 64,
        "semantic_revision": 1,
        "field_bindings": {
            "groupField": "f0",
            "timeField": "f1",
            "valueField": "f2",
        },
    }
    parameters = {
        "groups": ["A"],
        "start": "2024-01-01T00:00:00+00:00",
        "end": "2024-01-04T00:00:00+00:00",
        "frequency": "day",
        "min_samples": 3,
    }
    selected = model_profile()
    control = ControlService(
        setup["database"],
        agent_runtime_profile=profile(),
        agent_model_catalog=build_catalog([selected], selected["profile_id"]),
    )
    task = control.create_task(
        setup["actor"],
        setup["project_id"],
        "p64-real-agent-task",
        {
            "execution_mode": "AGENT",
            "spec": {
                "goal": "Compute completeness and trend",
                "requirements": [],
                "acceptance_spec": {
                    "profile": "p3-timeseries-v1",
                    "input": selection,
                    "parameters": parameters,
                    "required_outputs": [
                        "timeseries-completeness",
                        "statistics-linear-trend",
                    ],
                    "allow_not_evaluable": True,
                    "minimum_evaluable": 1,
                },
            },
            "budget": {"execution_count": 3, "max_in_flight": 2},
            "model_budget": model_budget(),
        },
    )
    targets = [target(value) for value in OPERATORS[:2]]
    control.grant(
        setup["actor"],
        setup["project_id"],
        "p64-action-grant",
        {
            "task_id": task["task_id"],
            "targets": targets,
            "resources": [asset],
            "derived_outputs": True,
            "acceptance_profiles": ["p3-timeseries-v1"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    with setup["database"].transaction() as uow:
        run = uow.agents.run_by_task(task["task_id"], required=True)
    context = {
        "project_id": setup["project_id"],
        "task_id": task["task_id"],
        "task_spec_revision": 1,
        "principal_id": setup["actor"].principal_id,
        "agent_run_id": run["agent_run_id"],
        "proposal_id": "proposal-one",
        "frame_id": "agent-frame-one",
    }
    backend = PlatformActionTools(setup["database"], data=Data(selection), execution=Execution())
    first = backend.execute(
        "execution.submit_operator",
        {"operator_id": "timeseries-completeness"},
        context,
        "stable-agent-tool-request",
    )
    second = backend.execute(
        "execution.submit_operator",
        {"operator_id": "timeseries-completeness"},
        context,
        "stable-agent-tool-request",
    )
    assert first.value == second.value
    with setup["database"].transaction() as uow:
        action = uow.actions.get(first.action_id)
        budget = uow.budgets.get(task["task_id"])
    assert action["token_id"] is None
    assert action["decision_source"] == "AGENT"
    assert action["frame_id"] == "stable-agent-tool-request"
    assert action["request"]["inputs"]["observations"] == selection
    assert action["request"]["parameters"] == parameters
    assert (budget.reserved, budget.in_flight) == (1, 1)

    from platform_core.application.science import Science

    assert (
        Science(setup["database"], Data(selection), Execution()).permit(
            first.action_id, "execution-one", "attempt-one"
        )["decision"]
        == "ALLOW"
    )


def test_action_membership_is_rechecked_before_backend_call(model_agent_setup):
    catalog = build_tool_catalog("actions")
    coordinator = AgentCoordinator(
        model_agent_setup["database"],
        context_compiler=ContextCompiler(catalog, max_context_tokens=20_000),
    )
    claim = coordinator.claim("p64-membership-worker", 30)
    frame = coordinator.prepare_frame(claim)
    accepted = coordinator.accept_proposal(claim, frame, proposal(frame))
    from platform_infrastructure import tables as t

    with model_agent_setup["database"].engine.begin() as connection:
        connection.execute(
            t.members.delete().where(
                t.members.c.project_id == model_agent_setup["project_id"],
                t.members.c.principal_id == model_agent_setup["actor"].principal_id,
            )
        )
    backend = Backend()
    import pytest
    from platform_core.domain.control import RuleViolation

    with pytest.raises(RuleViolation) as error:
        ActionToolGateway(model_agent_setup["database"], catalog, backend).execute_proposal(
            accepted["proposal_id"]
        )
    assert error.value.code == "RESOURCE_NOT_FOUND"
    assert backend.calls == []


def test_action_is_rejected_after_task_revision_changes(model_agent_setup):
    catalog = build_tool_catalog("actions")
    coordinator = AgentCoordinator(
        model_agent_setup["database"],
        context_compiler=ContextCompiler(catalog, max_context_tokens=20_000),
    )
    claim = coordinator.claim("p64-revision-worker", 30)
    frame = coordinator.prepare_frame(claim)
    accepted = coordinator.accept_proposal(claim, frame, proposal(frame))
    model_agent_setup["control"].revise(
        model_agent_setup["actor"],
        model_agent_setup["project_id"],
        model_agent_setup["task"]["task_id"],
        "p64-revise-after-decision",
        {
            "expected_control_version": 1,
            "expected_spec_revision": 1,
            "spec": {"goal": "Revised requirements", "requirements": []},
        },
    )
    backend = Backend()
    import pytest
    from platform_core.domain.control import RuleViolation

    with pytest.raises(RuleViolation) as error:
        ActionToolGateway(model_agent_setup["database"], catalog, backend).execute_proposal(
            accepted["proposal_id"]
        )
    assert error.value.code == "STALE_FRAME"
    assert backend.calls == []
