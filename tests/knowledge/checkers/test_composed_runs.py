from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from platform_core.application.acceptance import AcceptanceProcessor
from platform_core.application.catalog import OPEN_CHECK_TARGET, build_catalog
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.control import ControlService, digest
from platform_core.application.research import Research
from platform_core.application.science import Science
from platform_infrastructure import tables

from tests.knowledge.identity.test_contracts import EXPECTED


def analysis_request():
    return {
        "expected_spec_revision": 1,
        "supersedes_revision_id": None,
        "revision_reason": None,
        "question": "Is the published trend independently reproducible?",
        "research_objects": ["trend"],
        "spatial_scope": None,
        "temporal_scope": None,
        "input_roles": [],
        "methods": [],
        "primary_metrics": ["slope_per_year"],
        "comparison_rules": [],
        "data_splits": [],
        "stopping_rules": [],
        "allowed_deviations": [],
        "checker_versions": [],
        "observed_sources": [],
        "groundwater_comparison": None,
    }


def report_for(action):
    request = action["request"]
    plan, run = request["coverage_plan"], request["checker_run"]
    by_id = {item["item_id"]: item for item in plan["items"]}
    return {
        "schema_version": 2,
        "profile": "open-science-v1",
        "status": "PASS",
        "plan_digest": plan["plan_digest"],
        "run_id": run["run_id"],
        "capability_id": run["capability_id"],
        "capability_version": run["capability_version"],
        "capability_digest": run["capability_digest"],
        "input_contract_digest": run["input_contract_digest"],
        "output_contract_digest": run["output_contract_digest"],
        "items": [
            {
                "item_id": identity,
                "check_type": by_id[identity]["check_type"],
                "status": "PASS",
                "reason": "Trusted test capability verified its frozen input.",
            }
            for identity in run["item_ids"]
        ],
    }


def settle(database, action_id, execution_id):
    with database.transaction() as uow:
        action = uow.actions.get(action_id)
        reservation = uow.budgets.reservation(action_id)
        budget = uow.budgets.get(action["task_id"]).account(
            was_charged=reservation["charged"], charged=True, stopped=True
        )
        uow.budgets.save(budget)
        uow.budgets.update_reservation(
            action_id,
            source="execution",
            event_id="event-" + action_id,
            charged=True,
            stopped=True,
        )
        uow.actions.update(
            action_id,
            stopped=True,
            state="SUCCEEDED",
            execution_id=execution_id,
            external_revision=1,
        )
        return uow.actions.get(action_id)


class ReportsExecution:
    def __init__(self):
        self.reports = {}

    def json(self, method, path):
        execution_id = path.rsplit("/", 1)[-1]
        action, status = self.reports[execution_id]
        report = report_for(action)
        if status != "PASS":
            report["status"] = status
            for item in report["items"]:
                item["status"] = status
                item["reason"] = "Injected trusted checker failure."
        return {
            "action_id": action["action_id"],
            "request": action["request"],
            "state": "SUCCEEDED",
            "executed": True,
            "runtime": {"check_result": report},
        }


def setup_composed_acceptance(database, owner):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "r2-project", "R2")["project_id"]
    spec = {
        "profile": "open-science-v1",
        "delivery_class": "VERIFIED",
        "requirements": [
            {
                "requirement_id": "reproduce",
                "description": "Independently reproduce the trend",
                "necessary": True,
                "checks": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
            }
        ],
        "required_outputs": [{"role_id": "trend_result", "necessary": True, "expected": EXPECTED}],
        "allow_manual_review": False,
        "max_check_executions": 3,
    }
    task = control.create_task(
        actor,
        project,
        "r2-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "R2", "requirements": [], "acceptance_spec": spec},
            "budget": {"execution_count": 3, "max_in_flight": 1},
        },
    )
    research = Research(database)
    analysis = research.create_analysis(
        actor, project, task["task_id"], "analysis", analysis_request(), []
    )
    registration = research.register(
        actor,
        project,
        task["task_id"],
        analysis["revision_id"],
        "register",
        {"expected_spec_revision": 1},
    )
    control.grant(
        actor,
        project,
        "grant",
        {
            "task_id": task["task_id"],
            "targets": [OPEN_CHECK_TARGET],
            "resources": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
        },
    )
    ref = {
        "project_id": project,
        "asset_id": "asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "1" * 64,
    }
    representation = {
        "representation_id": "result",
        "format": "json",
        "digest": "sha256:" + "2" * 64,
        "size": 10,
        "schema_id": "p3-result-v1",
    }
    output_ref = {
        "kind": "OPERATOR_OUTPUT",
        "action_id": "result-action",
        "execution_id": "result-execution",
        "output_port": "result",
        "asset_ref": ref,
        "representation_id": "result",
        "representation_digest": representation["digest"],
        "schema_id": "p3-result-v1",
        "schema_source": "MANIFEST",
    }
    artifact = {
        "ref": ref,
        "manifest": {
            "kind": "OPERATOR_RESULT",
            "representations": [representation],
            "source": {
                "execution_id": "result-execution",
                "target": {
                    "kind": "OPERATOR",
                    "operator_id": "statistics-linear-trend",
                    "version_id": "1.0.0",
                },
            },
        },
    }
    with database.transaction() as uow:
        uow.science.add_candidate(
            {
                "deliverable_id": "deliverable",
                "project_id": project,
                "task_id": task["task_id"],
                "spec_revision": 1,
                "digest": digest(artifact),
                "payload": {
                    "identity_mode": "EXACT",
                    "artifacts": [artifact],
                    "executions": [],
                    "workflow_outputs": [],
                    "role_bindings": [{"role_id": "trend_result", "output_ref": output_ref}],
                },
                "identity_mode": "EXACT",
            }
        )
    receipt = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    ).accept(
        actor,
        project,
        task["task_id"],
        "accept",
        {
            "expected_spec_revision": 1,
            "deliverable_id": "deliverable",
            "analysis_registration_id": registration["registration_id"],
        },
    )
    return actor, control, project, task, receipt


def test_runtime_then_reproducer_use_one_envelope_and_complete_once(database, owner):
    actor, control, project, task, receipt = setup_composed_acceptance(database, owner)
    with database.transaction() as uow:
        runs = uow.science.check_runs(receipt["acceptance_id"])
        envelope = uow.science.check_envelope(receipt["acceptance_id"])
        budget = uow.budgets.get(task["task_id"])
    assert envelope["amount"] == 2
    assert [value["status"] for value in runs] == ["QUEUED", "PLANNED"]
    assert (budget.reserved, budget.in_flight) == (2, 1)

    execution = ReportsExecution()
    first = settle(database, runs[0]["action_id"], "runtime-execution")
    execution.reports["runtime-execution"] = (first, "PASS")
    AcceptanceProcessor(database, execution).process()
    with database.transaction() as uow:
        runs = uow.science.check_runs(receipt["acceptance_id"])
    assert [value["status"] for value in runs] == ["PASS", "QUEUED"]

    second = settle(database, runs[1]["action_id"], "reproduction-execution")
    execution.reports["reproduction-execution"] = (second, "PASS")
    AcceptanceProcessor(database, execution).process()
    view = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    ).checks(actor, project, task["task_id"], receipt["acceptance_id"])[0]
    assert view["status"] == "PASS"
    assert view["completion"]["validity"] == "CURRENT"
    assert {value["status"] for value in view["check_runs"]} == {"PASS"}
    budget = control.budget(actor, project, task["task_id"])
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(tables.completions)
                .where(tables.completions.c.task_id == task["task_id"])
            )
            == 1
        )


def test_pause_blocks_only_the_next_checker_admission(database, owner):
    actor, control, project, task, receipt = setup_composed_acceptance(database, owner)
    with database.transaction() as uow:
        runs = uow.science.check_runs(receipt["acceptance_id"])
    execution = ReportsExecution()
    first = settle(database, runs[0]["action_id"], "paused-runtime")
    execution.reports["paused-runtime"] = (first, "PASS")
    control.control(actor, project, task["task_id"], "pause", "pause", task["control_version"])
    AcceptanceProcessor(database, execution).process()
    with database.transaction() as uow:
        paused = uow.science.check_runs(receipt["acceptance_id"])
    assert [value["status"] for value in paused] == ["PASS", "PLANNED"]
    assert control.budget(actor, project, task["task_id"])["in_flight"] == 0

    current = control.task(actor, project, task["task_id"])
    control.control(
        actor,
        project,
        task["task_id"],
        "resume",
        "resume",
        current["control_version"],
    )
    AcceptanceProcessor(database, execution).process()
    with database.transaction() as uow:
        resumed = uow.science.check_runs(receipt["acceptance_id"])
    assert [value["status"] for value in resumed] == ["PASS", "QUEUED"]


def test_cancel_releases_unstarted_checker_allowance(database, owner):
    actor, control, project, task, receipt = setup_composed_acceptance(database, owner)
    with database.transaction() as uow:
        runs = uow.science.check_runs(receipt["acceptance_id"])
    execution = ReportsExecution()
    first = settle(database, runs[0]["action_id"], "cancelled-runtime")
    execution.reports["cancelled-runtime"] = (first, "PASS")
    control.control(actor, project, task["task_id"], "cancel", "cancel", task["control_version"])
    AcceptanceProcessor(database, execution).process()
    with database.transaction() as uow:
        cancelled = uow.science.check_runs(receipt["acceptance_id"])
        envelope = uow.science.check_envelope(receipt["acceptance_id"])
    assert [value["status"] for value in cancelled] == ["PASS", "SKIPPED"]
    assert envelope["released"] is True
    budget = control.budget(actor, project, task["task_id"])
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (1, 0, 0)


def test_stale_plan_cancels_queued_run_and_releases_unstarted_allowance(database, owner):
    actor, control, project, task, receipt = setup_composed_acceptance(database, owner)
    with database.transaction() as uow:
        runs = uow.science.check_runs(receipt["acceptance_id"])
        uow.science.mark_plan_stale(receipt["acceptance_id"])

    # The already admitted checker keeps its original identity and must first
    # settle its real execution fact.  A stale plan may not silently forget it.
    AcceptanceProcessor(database, ReportsExecution()).process()
    with database.transaction() as uow:
        queued = uow.science.check_runs(receipt["acceptance_id"])[0]
        assert uow.actions.get(queued["action_id"])["cancel_requested"] is True

    settle(database, runs[0]["action_id"], "stale-runtime")
    processor = AcceptanceProcessor(database, ReportsExecution())
    for _ in range(3):
        processor.process()

    with database.transaction() as uow:
        final_runs = uow.science.check_runs(receipt["acceptance_id"])
        envelope = uow.science.check_envelope(receipt["acceptance_id"])
    assert [value["status"] for value in final_runs] == ["ERROR", "SKIPPED"]
    assert envelope["released"] is True
    budget = control.budget(actor, project, task["task_id"])
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (1, 0, 0)


def test_error_retry_reuses_pass_and_runs_only_failed_capability(database, owner):
    actor, control, project, task, receipt = setup_composed_acceptance(database, owner)
    execution = ReportsExecution()
    with database.transaction() as uow:
        runs = uow.science.check_runs(receipt["acceptance_id"])
    first = settle(database, runs[0]["action_id"], "retry-runtime")
    execution.reports["retry-runtime"] = (first, "PASS")
    AcceptanceProcessor(database, execution).process()
    with database.transaction() as uow:
        runs = uow.science.check_runs(receipt["acceptance_id"])
    failed = settle(database, runs[1]["action_id"], "retry-reproducer-error")
    execution.reports["retry-reproducer-error"] = (failed, "ERROR")
    AcceptanceProcessor(database, execution).process()

    original = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    ).checks(actor, project, task["task_id"], receipt["acceptance_id"])[0]
    assert original["status"] == "ERROR"
    retry = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    ).accept(
        actor,
        project,
        task["task_id"],
        "retry-acceptance",
        {
            "expected_spec_revision": 1,
            "deliverable_id": "deliverable",
            "analysis_registration_id": original["analysis_registration_id"],
            "retry_of": receipt["acceptance_id"],
        },
    )
    with database.transaction() as uow:
        retry_runs = uow.science.check_runs(retry["acceptance_id"])
    assert [value["status"] for value in retry_runs] == ["REUSED", "QUEUED"]
    assert retry_runs[0]["action_id"] is None
    assert retry_runs[0]["retry_of_run_id"] == runs[0]["run_id"]
    assert retry_runs[1]["retry_of_run_id"] == runs[1]["run_id"]

    passed = settle(database, retry_runs[1]["action_id"], "retry-reproducer-pass")
    execution.reports["retry-reproducer-pass"] = (passed, "PASS")
    AcceptanceProcessor(database, execution).process()
    view = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    ).checks(actor, project, task["task_id"], retry["acceptance_id"])[0]
    assert view["status"] == "PASS"
    assert [value["status"] for value in view["check_runs"]] == ["REUSED", "PASS"]
    budget = control.budget(actor, project, task["task_id"])
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)
