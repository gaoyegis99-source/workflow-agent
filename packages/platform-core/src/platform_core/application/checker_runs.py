"""Admission rules for P5-R2 checker runs reserved by an acceptance envelope."""

from platform_core.domain.control import require
from platform_core.domain.workflows import acquire_slot, reserve_envelope


def target(run):
    return {
        "kind": "OPERATOR",
        "operator_id": "open-science-check",
        "version_id": "1.0.0",
        "execution_binding": {
            "kind": "LOCAL",
            "implementation_id": run["implementation_id"],
        },
    }


def request_for_run(project, task_id, spec_revision, spec, plan, run, candidate, registration_id):
    frozen = {
        key: run[key]
        for key in (
            "run_id",
            "run_key",
            "capability_id",
            "capability_version",
            "capability_digest",
            "implementation_id",
            "image_digest",
            "input_contract_digest",
            "output_contract_digest",
            "item_ids",
            "input_roles",
            "claim_revision_ids",
            "depends_on",
        )
    }
    return {
        "project_id": project,
        "task_id": task_id,
        "expected_spec_revision": spec_revision,
        "target": target(run),
        "resources": [],
        "parameters": {},
        "kind": "CHECK",
        "acceptance_spec": spec,
        "coverage_plan": plan,
        "checker_run": frozen | {"catalog_digest": plan["checker_catalog"]},
        "artifacts": candidate["payload"]["artifacts"],
        "role_bindings": candidate["payload"].get("role_bindings", []),
        "analysis_registration_id": registration_id,
    }


def reserve(uow, task, acceptance_id, actor, plan, amount):
    require(
        1 <= amount <= plan.get("max_check_executions", 4),
        "PRECONDITION_UNRESOLVED",
        "Coverage requires more checker executions than the task allows",
    )
    budget = reserve_envelope(uow.budgets.get(task.task_id), amount)
    uow.budgets.save(budget)
    uow.science.add_check_envelope(
        {
            "acceptance_id": acceptance_id,
            "task_id": task.task_id,
            "amount": amount,
            "released": False,
            "principal_id": actor.principal_id,
            "token_id": actor.token_id,
            "plan_digest": plan["plan_digest"],
            "catalog_digest": plan["checker_catalog"],
            "created_at": uow.now(),
        }
    )


def admit(uow, task, envelope, run, request):
    """Acquire one slot without reserving a second execution allowance."""

    task.admissible(request["expected_spec_revision"])
    frozen_target = target(run)
    require(request["target"] == frozen_target, "INVALID_BINDING", "Checker target changed")
    require(
        envelope["plan_digest"] == request["coverage_plan"]["plan_digest"]
        and envelope["catalog_digest"] == request["checker_run"]["catalog_digest"],
        "INVALID_BINDING",
        "Checker envelope differs from the frozen plan",
    )
    require(
        uow.tasks.permits(task.task_id, frozen_target, [], uow.now(), derived=True),
        "SCOPE_DENIED",
        "Checker run is outside the current acceptance grant",
        403,
    )
    budget = acquire_slot(uow.budgets.get(task.task_id))
    action_id, command_id = uow.new_id(), uow.new_id()
    uow.actions.add_command(
        {
            "command_id": command_id,
            "project_id": task.project_id,
            "task_id": task.task_id,
            "spec_revision": task.spec_revision,
            "status": "RECORDED",
            "action_id": action_id,
            "execution_id": None,
            "error_code": None,
            "error_message": None,
        }
    )
    uow.actions.add(
        {
            "action_id": action_id,
            "command_id": command_id,
            "task_id": task.task_id,
            "project_id": task.project_id,
            "spec_revision": task.spec_revision,
            "grant_epoch": task.grant_epoch,
            "principal_id": envelope["principal_id"],
            "token_id": envelope["token_id"],
            "frame_id": uow.new_id(),
            "decision_source": "CLIENT",
            "request": request,
            "send_started": False,
            "state": "RECORDED",
            "external_revision": 0,
            "execution_id": None,
            "stopped": False,
            "cancel_requested": False,
            "failure_count": 0,
        }
    )
    uow.budgets.save(budget)
    # Standard settlement now consumes one unit from the pre-reserved envelope.
    uow.budgets.reserve(action_id, task.task_id)
    uow.delivery.enqueue(
        uow.new_id(),
        task.task_id,
        "ACTION",
        {"action_id": action_id, "project_id": task.project_id},
    )
    uow.science.bind_check_run(run["run_id"], action_id)
    return action_id
