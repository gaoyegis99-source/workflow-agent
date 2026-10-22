from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from platform_core.application.control import ControlService
from platform_core.application.dispatch import Dispatcher
from platform_core.application.workflows import PackageProcessor, Workflows
from platform_core.domain.control import ExternalObservation, RuleViolation
from platform_core.domain.workflows import execution_id, invocation_id
from platform_infrastructure import tables

from .test_workflow_registry import PACKAGE, TARGET


class Accept:
    def validate(self, compiled):
        assert compiled["files"]


def setup_workflow(database, owner, *, maximum=1, allowance=2, derived=False, legacy=True):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "project", "P4")
    queued = Workflows(database).register(actor, project["project_id"], "package", PACKAGE)
    PackageProcessor(database, Accept(), owner="validator").process_one()
    package = Workflows(database).get(actor, project["project_id"], queued["package_id"])
    task = control.create_task(
        actor,
        project["project_id"],
        "task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "workflow", "requirements": []},
            "budget": {"execution_count": allowance, "max_in_flight": maximum},
        },
    )
    workflow_target = {"kind": "WORKFLOW", "package_ref": package["package_ref"]}
    control.grant(
        actor,
        project["project_id"],
        "grant",
        {
            "task_id": task["task_id"],
            "targets": [workflow_target, TARGET],
            "resources": [],
            "derived_outputs": derived,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    command = Workflows(database).admit(
        actor,
        project["project_id"],
        "run",
        {
            "task_id": task["task_id"],
            "expected_spec_revision": 1,
            "target": workflow_target,
            "inputs": {},
            "resources": [],
            "max_node_executions": allowance,
        },
    )
    if legacy:
        # These budget-focused probes preserve the pre-R4.2 arbitrary-node fixture.
        # Production actions are immutable; the raw update exists only to exercise
        # compatibility with an already persisted workflow that has no plan.
        with database.engine.begin() as connection:
            request = dict(
                connection.scalar(
                    sa.select(tables.actions.c.request).where(
                        tables.actions.c.action_id == command["action_id"]
                    )
                )
            )
            request.pop("execution_plan")
            connection.execute(
                tables.actions.update()
                .where(tables.actions.c.action_id == command["action_id"])
                .values(request=request)
            )
    return actor, project["project_id"], task["task_id"], command


def dispatch_root(database, action_id, root="root"):
    dispatcher = Dispatcher(database)
    claim = None
    for _ in range(10):
        current = dispatcher.claim("worker", 30)
        assert current is not None
        prepared = dispatcher.prepare(current)
        if prepared is not None and prepared["action_id"] == action_id:
            claim = current
            break
    assert claim is not None
    dispatcher.observe(claim, ExternalObservation(root + ":1", action_id, 1, "QUEUED", root, False))


def node_request(index):
    return {
        "call_path": ["calculate"],
        "scatter_indices": [index],
        "inputs": {"value": index},
        "engine_job_id": "job-" + str(index),
        "epoch": 1,
    }


def test_parent_reserves_without_slot_and_serial_nodes_finish(database, owner):
    actor, project, task, command = setup_workflow(database, owner)
    budget = ControlService(database).budget(actor, project, task)
    assert (budget["reserved"], budget["in_flight"], budget["used"]) == (2, 0, 0)
    dispatch_root(database, command["action_id"])

    workflows = Workflows(database)
    first = invocation_id("root", ["calculate"], [0])
    second = invocation_id("root", ["calculate"], [1])
    assert (
        workflows.permit_node(command["action_id"], "root", first, node_request(0))["decision"]
        == "ALLOW"
    )
    # A replay never consumes a second slot.
    assert (
        workflows.permit_node(command["action_id"], "root", first, node_request(0))["decision"]
        == "ALLOW"
    )
    assert (
        workflows.permit_node(command["action_id"], "root", second, node_request(1))["decision"]
        == "WAIT"
    )
    assert ControlService(database).budget(actor, project, task)["in_flight"] == 1

    workflows.observe_node(
        command["action_id"],
        "root",
        first,
        {
            "revision": 1,
            "state": "SUCCEEDED",
            "executed": True,
            "stopped": True,
            "execution_id": "child0",
            "attempt_id": "attempt0",
        },
    )
    assert (
        workflows.permit_node(command["action_id"], "root", second, node_request(1))["decision"]
        == "ALLOW"
    )
    workflows.observe_node(
        command["action_id"],
        "root",
        second,
        {
            "revision": 1,
            "state": "REJECTED",
            "executed": False,
            "stopped": True,
            "execution_id": "child1",
            "attempt_id": "attempt1",
        },
    )
    budget = ControlService(database).budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (1, 1, 0)

    # Make the root reconciliation immediately claimable, then close the
    # envelope. The unused second allowance is released exactly once.
    with database.engine.begin() as connection:
        connection.execute(
            tables.outbox.update()
            .where(tables.outbox.c.done.is_(False))
            .values(available_at=sa.func.clock_timestamp())
        )
    dispatcher = Dispatcher(database)
    claim = dispatcher.claim("finisher", 30)
    prepared = dispatcher.prepare(claim)
    assert prepared["method"] == "query"
    dispatcher.observe(
        claim,
        ExternalObservation("root:2", command["action_id"], 2, "SUCCEEDED", "root", True),
    )
    budget = ControlService(database).budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (1, 0, 0)


def test_node_observation_is_monotonic_and_step_identity_is_public(database, owner):
    actor, project, _, command = setup_workflow(database, owner, maximum=2, allowance=2)
    dispatch_root(database, command["action_id"])
    workflows = Workflows(database)
    node = invocation_id("root", ["calculate"], [0])
    assert (
        workflows.permit_node(command["action_id"], "root", node, node_request(0))["decision"]
        == "ALLOW"
    )
    fact = {
        "revision": 2,
        "state": "RUNNING",
        "executed": True,
        "stopped": False,
        "execution_id": "child",
        "attempt_id": "attempt",
    }
    assert workflows.observe_node(command["action_id"], "root", node, fact)
    assert not workflows.observe_node(command["action_id"], "root", node, fact | {"revision": 1})
    steps = workflows.steps(actor, project, "root")
    assert steps == [
        {
            "node_id": node,
            "execution_id": "child",
            "call_path": ["calculate"],
            "scatter_indices": [0],
            "state": "RUNNING",
            "executed": True,
            "attempt_id": "attempt",
            "engine_job_id": "job-0",
        }
    ]


def test_changed_engine_job_id_does_not_change_node_or_budget(database, owner):
    actor, project, task, command = setup_workflow(database, owner, maximum=1, allowance=1)
    dispatch_root(database, command["action_id"])
    workflows = Workflows(database)
    node = invocation_id("root", ["calculate"], [0])
    first = node_request(0)
    assert workflows.permit_node(command["action_id"], "root", node, first)["decision"] == "ALLOW"
    replay = first | {
        "engine_job_id": "recovered-job",
        "engine_binding": {"kind": "toil", "id": "recovered-job", "epoch": 2},
    }
    assert workflows.permit_node(command["action_id"], "root", node, replay)["decision"] == "ALLOW"
    budget = ControlService(database).budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (0, 1, 1)
    assert workflows.steps(actor, project, "root")[0]["node_id"] == node


def test_new_workflow_freezes_plan_and_rejects_unplanned_node(database, owner):
    _, _, _, command = setup_workflow(database, owner, maximum=1, allowance=1, legacy=False)
    root = execution_id(command["action_id"])
    dispatch_root(database, command["action_id"], root)
    with database.transaction() as uow:
        plan = uow.actions.get(command["action_id"])["request"]["execution_plan"]
    assert plan["schema_version"] == 1
    assert len(plan["nodes"]) == 1
    planned = plan["nodes"][0]
    assert planned["node_id"] == invocation_id(root, ["calculate"], [])
    assert (
        Workflows(database).permit_node(
            command["action_id"],
            root,
            planned["node_id"],
            node_request(0) | {"scatter_indices": []},
        )["decision"]
        == "ALLOW"
    )
    unplanned = invocation_id(root, ["calculate"], [0])
    with pytest.raises(RuleViolation) as raised:
        Workflows(database).permit_node(command["action_id"], root, unplanned, node_request(0))
    assert getattr(raised.value, "code", None) == "INVALID_BINDING"


def test_concurrent_nodes_cannot_exceed_last_slot(database, owner):
    _, _, _, command = setup_workflow(database, owner, maximum=1, allowance=2)
    dispatch_root(database, command["action_id"])
    workflows = Workflows(database)

    def permit(index):
        node = invocation_id("root", ["calculate"], [index])
        return workflows.permit_node(command["action_id"], "root", node, node_request(index))[
            "decision"
        ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(permit, [0, 1]))
    assert sorted(decisions) == ["ALLOW", "WAIT"]


def test_pause_cancel_and_expansion_limit_are_checked_at_each_node(database, owner):
    actor, project, task, command = setup_workflow(database, owner, maximum=2, allowance=1)
    dispatch_root(database, command["action_id"])
    control = ControlService(database)
    control.control(actor, project, task, "pause", "pause", 1)
    workflows = Workflows(database)
    first = invocation_id("root", ["calculate"], [0])
    assert (
        workflows.permit_node(command["action_id"], "root", first, node_request(0))["decision"]
        == "WAIT"
    )
    control.control(actor, project, task, "resume", "resume", 2)
    assert (
        workflows.permit_node(command["action_id"], "root", first, node_request(0))["decision"]
        == "ALLOW"
    )
    second = invocation_id("root", ["calculate"], [1])
    denied = workflows.permit_node(command["action_id"], "root", second, node_request(1))
    assert denied == {"decision": "DENY", "reportable": False, "reason": "NODE_LIMIT"}
    control.control(actor, project, task, "cancel", "cancel", 3)
    assert (
        workflows.permit_node(command["action_id"], "root", first, node_request(0))["decision"]
        == "DENY"
    )


@pytest.mark.parametrize("derived,decision", [(False, "DENY"), (True, "ALLOW")])
def test_private_node_input_requires_explicit_derived_output_permission(
    database, owner, derived, decision
):
    _, _, _, command = setup_workflow(database, owner, maximum=1, allowance=1, derived=derived)
    dispatch_root(database, command["action_id"])
    workflows = Workflows(database)
    node = invocation_id("root", ["calculate"], [0])
    request = node_request(0) | {"uses_derived": True}
    assert (
        workflows.permit_node(command["action_id"], "root", node, request)["decision"] == decision
    )


@pytest.mark.parametrize("change", ["expire", "revise"])
def test_node_rechecks_expiry_and_requirement_revision(database, owner, change):
    actor, project, task, command = setup_workflow(database, owner, maximum=1, allowance=1)
    dispatch_root(database, command["action_id"])
    if change == "expire":
        with database.engine.begin() as connection:
            connection.execute(
                tables.grants.update().values(
                    expires_at=sa.func.clock_timestamp() - sa.text("interval '1 second'")
                )
            )
    else:
        ControlService(database).revise(
            actor,
            project,
            task,
            "revise",
            {
                "expected_spec_revision": 1,
                "expected_control_version": 1,
                "spec": {"goal": "revised while queued", "requirements": []},
            },
        )
    workflows = Workflows(database)
    node = invocation_id("root", ["calculate"], [0])
    denied = workflows.permit_node(command["action_id"], "root", node, node_request(0))
    assert denied["decision"] == "DENY" and not denied["reportable"]
    assert workflows.steps(actor, project, "root")[0]["state"] == "REJECTED"
    budget = ControlService(database).budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (0, 1, 0)
