"""Consume only authenticated checker execution facts; completion is a local task transaction."""

from dataclasses import replace

from platform_core.application.control import ControlService, digest
from platform_core.application.open_acceptance import (
    compile_coverage,
    outcome,
    validate_capability_report,
    validate_checker_report,
)
from platform_core.domain.control import RuleViolation, require


class AcceptanceProcessor:
    def __init__(self, store, execution):
        self.store, self.execution = store, execution

    def process(self):
        changed = False
        while self._process_r2():
            changed = True
        with self.store.transaction() as uow:
            pending = uow.science.pending_checks()
        for item in pending:
            with self.store.transaction() as uow:
                action = uow.actions.get(item["action_id"])
                task = uow.tasks.get(item["project_id"], item["task_id"])
            if not action["stopped"]:
                continue
            report = item["report"]
            if report is None:
                if action["execution_id"] is None:
                    report = {
                        "status": "ERROR",
                        "category": "NOT_RUN",
                        "executed": False,
                        "execution_state": action["state"],
                    }
                else:
                    execution = self.execution.json(
                        "GET",
                        f"/internal/projects/{item['project_id']}/executions/{action['execution_id']}",
                    )
                    require(
                        execution["action_id"] == action["action_id"]
                        and execution["request"] == action["request"],
                        "INVALID_BINDING",
                        "Checker execution provenance differs",
                    )
                    if execution["state"] == "SUCCEEDED":
                        report = execution["runtime"]["check_result"]
                        profile = action["request"]["acceptance_spec"]["profile"]
                        allowed = (
                            ("PASS", "FAIL", "NOT_EVALUABLE")
                            if profile == "p3-timeseries-v1"
                            else ("PASS", "FAIL", "INDETERMINATE", "REVIEW_REQUIRED", "ERROR")
                        )
                        require(
                            report["profile"] == profile and report["status"] in allowed,
                            "INVALID_BINDING",
                            "Invalid check report",
                        )
                    else:
                        report = {
                            "status": "CANCELLED" if execution["state"] == "CANCELLED" else "ERROR",
                            "category": execution["runtime"].get("error") or "NOT_RUN",
                            "executed": execution["executed"],
                            "execution_state": execution["state"],
                        }
                report = report | {
                    "action_id": action["action_id"],
                    "execution_id": action["execution_id"],
                    "spec_revision": item["spec_revision"],
                    "deliverable_id": item["deliverable_id"],
                }
            with self.store.transaction() as uow:
                current = uow.tasks.get(item["project_id"], item["task_id"], lock=True)
                record = uow.science.check(item["project_id"], item["acceptance_id"])
                plan = uow.science.plan(item["acceptance_id"])
                if plan is not None:
                    candidate = uow.science.candidate(item["project_id"], item["deliverable_id"])
                    spec = uow.tasks.spec(item["task_id"], item["spec_revision"])["acceptance_spec"]
                    try:
                        arguments = (
                            spec,
                            candidate,
                            uow.research.registrations(item["task_id"]),
                            uow.research.claims(item["task_id"]),
                            uow.research.policy_decisions(item["task_id"]),
                            uow.research.provenance(item["task_id"]),
                            uow.research.analyses(item["task_id"]),
                        )
                        current_plan, _ = (
                            compile_coverage(*arguments)
                            if plan["plan"].get("schema_version") == 1
                            else compile_coverage(
                                *arguments, record.get("analysis_registration_id")
                            )
                        )
                    except RuleViolation:
                        current_plan = None
                    if (
                        plan["status"] != "ACTIVE"
                        or current.spec_revision != item["spec_revision"]
                        or current_plan is None
                        or current_plan["source_digest"] != plan["source_digest"]
                    ):
                        uow.science.mark_plan_stale(item["acceptance_id"])
                        changed = True
                        continue
                if record["report"] is None:
                    if plan is not None and report.get("profile") == "open-science-v1":
                        validate_checker_report(plan["plan"], report)
                        uow.science.apply_item_results(plan["plan_id"], report["items"])
                        plan = uow.science.plan(item["acceptance_id"])
                        status, limitations = outcome(plan["delivery_class"], plan["items"])
                        limitations = list(
                            dict.fromkeys(plan["plan"].get("limitations", []) + limitations)
                        )
                        report = report | {
                            "status": status,
                            "delivery_class": plan["delivery_class"],
                            "limitations": limitations,
                            "coverage_plan_revision": plan["revision"],
                        }
                        uow.science.update_check(
                            item["acceptance_id"], status, report, digest(report)
                        )
                    else:
                        uow.science.report(
                            item["acceptance_id"], report["status"], report, digest(report)
                        )
                    record = record | {
                        "status": report["status"],
                        "report": report,
                        "report_digest": digest(report),
                    }
                    changed = True
                if (
                    record["status"] != "PASS"
                    or current.lifecycle != "OPEN"
                    or current.control != "ACTIVE"
                    or current.control_version != task.control_version
                    or current.spec_revision != item["spec_revision"]
                    or uow.actions.active(current.task_id)
                ):
                    continue
                candidate = uow.science.candidate(item["project_id"], item["deliverable_id"])
                require(
                    candidate["spec_revision"] == current.spec_revision,
                    "INVALID_BINDING",
                    "Candidate revision changed",
                )
                # Task lock serializes completion with cancel/pause/revision and action admission.
                uow.science.complete(current, record)
                uow.science.set_completion_validity(current.task_id, "CURRENT")
                current = replace(
                    current, lifecycle="COMPLETED", control_version=current.control_version + 1
                )
                uow.tasks.save(current)
                ControlService._event(uow, current, "TASK_COMPLETED")
                changed = True
        return changed

    @staticmethod
    def _failure_report(plan, run, status, reason):
        by_id = {item["item_id"]: item for item in plan["items"]}
        return {
            "schema_version": 2,
            "profile": "open-science-v1",
            "status": status,
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
                    "status": status,
                    "reason": reason,
                }
                for identity in run["item_ids"]
            ],
        }

    @staticmethod
    def _release_envelope(uow, acceptance_id, task_id):
        remaining = uow.science.release_check_envelope(acceptance_id)
        if remaining:
            budget = uow.budgets.get(task_id)
            require(
                budget.reserved >= remaining,
                "INVALID_BINDING",
                "Checker envelope release exceeds reserved budget",
            )
            uow.budgets.save(replace(budget, reserved=budget.reserved - remaining))

    def _process_r2(self):
        """Advance one dependency-safe step for each composed R2 acceptance."""

        from platform_core.application.checker_runs import admit, request_for_run

        with self.store.transaction() as uow:
            pending = uow.science.active_r2_checks()
        changed = False
        for acceptance in pending:
            with self.store.transaction() as uow:
                task = uow.tasks.get(acceptance["project_id"], acceptance["task_id"], lock=True)
                record = uow.science.check(acceptance["project_id"], acceptance["acceptance_id"])
                plan_row = uow.science.plan(acceptance["acceptance_id"])
                if plan_row is None or plan_row["plan"].get("schema_version") != 3:
                    continue
                plan = plan_row["plan"]
                runs = uow.science.check_runs(acceptance["acceptance_id"], lock=True)
                if record["status"] == "STALE" or plan_row["status"] == "STALE":
                    progressed = False
                    waiting_for_stop = False
                    for run in runs:
                        if run["status"] == "PLANNED":
                            report = self._failure_report(
                                plan, run, "ERROR", "COVERAGE_PLAN_BECAME_STALE"
                            )
                            uow.science.skip_check_run(run["run_id"], "SKIPPED", report)
                            progressed = True
                        elif run["status"] == "QUEUED":
                            action = uow.actions.get(run["action_id"])
                            if action["stopped"]:
                                report = self._failure_report(
                                    plan, run, "ERROR", "COVERAGE_PLAN_BECAME_STALE"
                                )
                                uow.science.finish_check_run(run["run_id"], "ERROR", report)
                                progressed = True
                            elif not action["cancel_requested"]:
                                uow.actions.update(run["action_id"], cancel_requested=True)
                                progressed = True
                            else:
                                waiting_for_stop = True
                    if progressed:
                        changed = True
                        continue
                    if waiting_for_stop:
                        continue
                    self._release_envelope(uow, acceptance["acceptance_id"], acceptance["task_id"])
                    continue
                candidate = uow.science.candidate(
                    acceptance["project_id"], acceptance["deliverable_id"]
                )
                spec = uow.tasks.spec(acceptance["task_id"], acceptance["spec_revision"])[
                    "acceptance_spec"
                ]
                try:
                    current_plan, _ = compile_coverage(
                        spec,
                        candidate,
                        uow.research.registrations(acceptance["task_id"]),
                        uow.research.claims(acceptance["task_id"]),
                        uow.research.policy_decisions(acceptance["task_id"]),
                        uow.research.provenance(acceptance["task_id"]),
                        uow.research.analyses(acceptance["task_id"]),
                        record.get("analysis_registration_id"),
                        plan["capability_catalog"],
                    )
                except RuleViolation:
                    current_plan = None
                if (
                    plan_row["status"] != "ACTIVE"
                    or task.spec_revision != acceptance["spec_revision"]
                    or current_plan is None
                    or current_plan["source_digest"] != plan_row["source_digest"]
                ):
                    for run in runs:
                        if run["status"] == "QUEUED":
                            action = uow.actions.get(run["action_id"])
                            if not action["stopped"] and not action["cancel_requested"]:
                                uow.actions.update(run["action_id"], cancel_requested=True)
                    uow.science.mark_plan_stale(acceptance["acceptance_id"])
                    changed = True
                    continue

            # Fetch the external report without holding a database transaction.
            queued = next((run for run in runs if run["status"] == "QUEUED"), None)
            if queued is not None:
                with self.store.transaction() as uow:
                    action = uow.actions.get(queued["action_id"])
                if not action["stopped"]:
                    continue
                if action["execution_id"] is None:
                    report = self._failure_report(
                        plan, queued, "ERROR", "Checker was rejected before a trusted run began."
                    )
                else:
                    execution = self.execution.json(
                        "GET",
                        f"/internal/projects/{acceptance['project_id']}/executions/"
                        f"{action['execution_id']}",
                    )
                    require(
                        execution["action_id"] == action["action_id"]
                        and execution["request"] == action["request"],
                        "INVALID_BINDING",
                        "Checker execution provenance differs",
                    )
                    report = (
                        execution["runtime"]["check_result"]
                        if execution["state"] == "SUCCEEDED"
                        else self._failure_report(
                            plan,
                            queued,
                            "ERROR",
                            "Trusted checker execution did not produce a valid report.",
                        )
                    )
                validate_capability_report(plan, queued, report)
                with self.store.transaction() as uow:
                    locked = uow.science.check_run(queued["run_id"], lock=True)
                    if locked["status"] != "QUEUED":
                        continue
                    uow.science.apply_item_results(plan_row["plan_id"], report["items"])
                    uow.science.finish_check_run(queued["run_id"], report["status"], report)
                changed = True
                continue

            terminal = {
                run["capability_id"]: run["status"]
                for run in runs
                if run["status"] not in {"PLANNED", "QUEUED"}
            }
            planned = [run for run in runs if run["status"] == "PLANNED"]
            # Cancellation is authoritative for work that never acquired an
            # Action.  Queued Actions continue through the normal dispatcher so
            # their real executed/not-executed fact settles the budget first.
            if planned and task.lifecycle != "OPEN":
                run = planned[0]
                report = self._failure_report(
                    plan,
                    run,
                    "ERROR",
                    "CANCELLED_BEFORE_CHECKER_START",
                )
                with self.store.transaction() as uow:
                    locked = uow.science.check_run(run["run_id"], lock=True)
                    if locked["status"] != "PLANNED":
                        continue
                    uow.science.apply_item_results(plan_row["plan_id"], report["items"])
                    uow.science.skip_check_run(run["run_id"], "SKIPPED", report)
                changed = True
                continue
            # A pause only prevents new checker Actions. Existing Actions are
            # still observed above and preserve their external execution facts.
            if planned and task.control == "PAUSED":
                continue
            admitted = False
            for run in planned:
                dependency_states = [terminal.get(value) for value in run["depends_on"]]
                if any(
                    value in {"FAIL", "ERROR", "INDETERMINATE", "CANCELLED"}
                    for value in dependency_states
                ):
                    status = (
                        "FAIL"
                        if "FAIL" in dependency_states or "INDETERMINATE" in dependency_states
                        else "ERROR"
                    )
                    reason = (
                        "SUPPORTING_RESULT_FAILED_REPRODUCTION"
                        if run["capability_id"] == "p3-descriptive-claim-support-v1"
                        else "UPSTREAM_CHECKER_DID_NOT_PASS"
                    )
                    report = self._failure_report(plan, run, status, reason)
                    with self.store.transaction() as uow:
                        uow.science.apply_item_results(plan_row["plan_id"], report["items"])
                        uow.science.skip_check_run(run["run_id"], "SKIPPED", report)
                    changed = True
                    admitted = True
                    break
                if not all(value in {"PASS", "REUSED"} for value in dependency_states):
                    continue
                with self.store.transaction() as uow:
                    current = uow.tasks.get(
                        acceptance["project_id"], acceptance["task_id"], lock=True
                    )
                    envelope = uow.science.check_envelope(acceptance["acceptance_id"], lock=True)
                    locked = uow.science.check_run(run["run_id"], lock=True)
                    if locked["status"] != "PLANNED":
                        continue
                    try:
                        admit(
                            uow,
                            current,
                            envelope,
                            locked,
                            request_for_run(
                                acceptance["project_id"],
                                acceptance["task_id"],
                                acceptance["spec_revision"],
                                spec,
                                plan,
                                locked,
                                candidate,
                                record.get("analysis_registration_id"),
                            ),
                        )
                    except RuleViolation as error:
                        if error.code == "BUDGET_BLOCKED":
                            continue
                        if error.code in {"SCOPE_DENIED", "PRECONDITION_UNRESOLVED"}:
                            report = self._failure_report(
                                plan,
                                locked,
                                "ERROR",
                                "CHECKER_PERMISSION_DENIED"
                                if error.code == "SCOPE_DENIED"
                                else "CHECKER_ADMISSION_INVALIDATED",
                            )
                            uow.science.apply_item_results(plan_row["plan_id"], report["items"])
                            uow.science.skip_check_run(locked["run_id"], "SKIPPED", report)
                        else:
                            raise
                changed = True
                admitted = True
                break
            if admitted:
                continue
            if planned:
                continue

            with self.store.transaction() as uow:
                current = uow.tasks.get(acceptance["project_id"], acceptance["task_id"], lock=True)
                current_runs = uow.science.check_runs(acceptance["acceptance_id"], lock=True)
                if any(run["status"] in {"PLANNED", "QUEUED"} for run in current_runs):
                    continue
                self._release_envelope(uow, acceptance["acceptance_id"], current.task_id)
                refreshed = uow.science.plan(acceptance["acceptance_id"])
                status, limitations = outcome(refreshed["delivery_class"], refreshed["items"])
                report = {
                    "schema_version": 2,
                    "profile": "open-science-v1",
                    "status": status,
                    "delivery_class": refreshed["delivery_class"],
                    "plan_digest": plan["plan_digest"],
                    "coverage_plan_revision": refreshed["revision"],
                    "limitations": list(dict.fromkeys(plan.get("limitations", []) + limitations)),
                    "checker_runs": [
                        {
                            "run_id": run["run_id"],
                            "capability_id": run["capability_id"],
                            "status": run["status"],
                            "action_id": run["action_id"],
                            "report_digest": run["report_digest"],
                        }
                        for run in current_runs
                    ],
                }
                if record["status"] == "QUEUED":
                    uow.science.update_check(
                        acceptance["acceptance_id"], status, report, digest(report)
                    )
                    changed = True
                completed_record = record | {
                    "status": status,
                    "report": report,
                    "report_digest": digest(report),
                }
                if (
                    status == "PASS"
                    and current.lifecycle == "OPEN"
                    and current.control == "ACTIVE"
                    and current.spec_revision == acceptance["spec_revision"]
                    and not uow.actions.active(current.task_id)
                ):
                    uow.science.complete(current, completed_record)
                    uow.science.set_completion_validity(current.task_id, "CURRENT")
                    current = replace(
                        current,
                        lifecycle="COMPLETED",
                        control_version=current.control_version + 1,
                    )
                    uow.tasks.save(current)
                    ControlService._event(uow, current, "TASK_COMPLETED")
                    changed = True
        return changed
