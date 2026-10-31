"""Candidate identity and acceptance lifecycle flow for Science."""

from platform_core.application.control import ControlService
from platform_core.application.output_identity import (
    exact_deliverable_digest,
    exact_output_requirements,
    output_facts,
    require_expected,
)
from platform_core.application.science_support import ScienceFlow
from platform_core.domain.control import require


class ScienceAcceptanceFlow(ScienceFlow):
    @staticmethod
    def _selected_output(receipt, port, path):
        output = receipt.get("outputs", {}).get(port)
        for part in path:
            require(
                isinstance(output, list if isinstance(part, int) else dict),
                "INVALID_BINDING",
                "Workflow output path is invalid",
            )
            try:
                output = output[part]
            except (IndexError, KeyError):
                require(False, "INVALID_BINDING", "Workflow output path is invalid")
        require(
            isinstance(output, dict) and "asset" in output and "representation_id" in output,
            "INVALID_BINDING",
            "Selected workflow output is not an asset representation",
        )
        return output

    def _canonical_role_binding(self, actor, project, task_id, spec_revision, binding):
        supplied = binding["output_ref"]
        if supplied["kind"] == "OPERATOR_OUTPUT":
            value = self.execution_view(actor, project, supplied["execution_id"])
            require(
                value["task_id"] == task_id
                and value["state"] == "SUCCEEDED"
                and value["runtime"].get("publication") == "COMMITTED",
                "INVALID_BINDING",
                "Exact output must be a published task execution",
            )
            receipt = value["runtime"]["publication_receipt"]
            ref = receipt.get("asset")
            require(ref is not None, "INVALID_BINDING", "Execution has no direct published asset")
            with self.store.transaction() as uow:
                action = uow.actions.by_execution(project, supplied["execution_id"])
            asset = self.data_call(
                actor, project, "GET", f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
            )
            facts = output_facts(asset["manifest"], supplied["output_port"])
            canonical = {
                "kind": "OPERATOR_OUTPUT",
                "action_id": action["action_id"],
                "execution_id": supplied["execution_id"],
                "output_port": supplied["output_port"],
                "asset_ref": ref,
                "representation_id": supplied["output_port"],
                "representation_digest": facts["representation"]["digest"],
                "schema_id": facts["schema_id"],
                "schema_source": facts["schema_source"],
            }
        else:
            value = self.execution_view(actor, project, supplied["parent_execution_id"])
            require(
                value["task_id"] == task_id
                and value["state"] == "SUCCEEDED"
                and value["runtime"].get("publication") == "COMMITTED",
                "INVALID_BINDING",
                "Exact output must be a published task workflow",
            )
            output = self._selected_output(
                value["runtime"]["publication_receipt"],
                supplied["published_output_port"],
                supplied["published_path"],
            )
            ref = output["asset"]
            representation_id = output["representation_id"]
            asset = self.data_call(
                actor, project, "GET", f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
            )
            with self.store.transaction() as uow:
                parent = uow.actions.by_execution(project, supplied["parent_execution_id"])
                nodes = uow.workflows.nodes(parent["action_id"])
            member_id = asset["manifest"].get("workflow", {}).get("member_id")
            node = next((item for item in nodes if item["node_id"] == member_id), None)
            require(
                node is not None and node["observation"] is not None,
                "INVALID_BINDING",
                "Workflow output has no authoritative node",
            )
            node_execution = node["observation"].get("execution_id")
            request = node["request"]
            facts = output_facts(asset["manifest"], representation_id)
            canonical = {
                "kind": "WORKFLOW_OUTPUT",
                "parent_action_id": parent["action_id"],
                "parent_execution_id": supplied["parent_execution_id"],
                "node_action_id": node["node_id"],
                "node_execution_id": node_execution,
                "call_path": request["call_path"],
                "scatter_path": request["scatter_indices"],
                "published_output_port": supplied["published_output_port"],
                "published_path": supplied["published_path"],
                "output_port": representation_id,
                "asset_ref": ref,
                "representation_id": representation_id,
                "representation_digest": facts["representation"]["digest"],
                "schema_id": facts["schema_id"],
                "schema_source": facts["schema_source"],
            }
        require(
            asset["ref"] == ref
            and asset["manifest"]["source"]["task_ref"]
            == {"task_id": task_id, "spec_revision": spec_revision}
            and supplied == canonical,
            "INVALID_BINDING",
            "Caller output identity differs from authoritative publication facts",
        )
        return (
            {"role_id": binding["role_id"], "output_ref": canonical},
            {
                key: facts[key]
                for key in (
                    "result_kind",
                    "operator_id",
                    "operator_version",
                    "output_port",
                    "schema_id",
                )
            },
            {"ref": ref, "manifest": asset["manifest"]},
        )

    def operator_role_binding(
        self, actor, project, task_id, spec_revision, role_id, execution_id, output_port
    ):
        """Build an exact direct-output identity only from authoritative facts."""
        value = self.execution_view(actor, project, execution_id)
        require(
            value["task_id"] == task_id
            and value["state"] == "SUCCEEDED"
            and value["runtime"].get("publication") == "COMMITTED",
            "INVALID_BINDING",
            "Exact output must be a published task execution",
        )
        receipt = value["runtime"]["publication_receipt"]
        ref = receipt.get("asset")
        require(ref is not None, "INVALID_BINDING", "Execution has no direct published asset")
        with self.store.transaction() as uow:
            action = uow.actions.by_execution(project, execution_id)
        asset = self.data_call(
            actor, project, "GET", f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
        )
        facts = output_facts(asset["manifest"], output_port)
        require(
            asset["ref"] == ref
            and asset["manifest"]["source"]["task_ref"]
            == {"task_id": task_id, "spec_revision": spec_revision},
            "INVALID_BINDING",
            "Published output belongs to another task revision",
        )
        return {
            "role_id": role_id,
            "output_ref": {
                "kind": "OPERATOR_OUTPUT",
                "action_id": action["action_id"],
                "execution_id": execution_id,
                "output_port": output_port,
                "asset_ref": ref,
                "representation_id": output_port,
                "representation_digest": facts["representation"]["digest"],
                "schema_id": facts["schema_id"],
                "schema_source": facts["schema_source"],
            },
        }

    def candidate(self, actor, project, task_id, key, request):
        if request.get("workflow_outputs") == []:
            request = {key: value for key, value in request.items() if key != "workflow_outputs"}
        self.authorize(actor, project, "EDITOR")
        artifacts = []
        canonical_bindings = []
        binding_facts = {}
        for role_binding in request.get("role_bindings", []):
            canonical, facts, artifact = self._canonical_role_binding(
                actor,
                project,
                task_id,
                request["expected_spec_revision"],
                role_binding,
            )
            canonical_bindings.append(canonical)
            binding_facts[canonical["role_id"]] = facts
            if all(item["ref"] != artifact["ref"] for item in artifacts):
                artifacts.append(artifact)
        for execution_id in sorted(request["executions"]):
            value = self.execution_view(actor, project, execution_id)
            require(
                value["task_id"] == task_id
                and value["state"] == "SUCCEEDED"
                and value["runtime"].get("publication") == "COMMITTED",
                "INVALID_BINDING",
                "Candidate must be a published task execution",
            )
            ref = value["runtime"]["publication_receipt"]["asset"]
            asset = self.data_call(
                actor, project, "GET", f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
            )
            require(
                asset["ref"] == ref
                and asset["manifest"]["source"]["task_ref"]
                == {"task_id": task_id, "spec_revision": request["expected_spec_revision"]},
                "INVALID_BINDING",
                "Candidate belongs to another task revision",
            )
            artifacts.append({"ref": ref, "manifest": asset["manifest"]})
        for selection in request.get("workflow_outputs", []):
            value = self.execution_view(actor, project, selection["execution_id"])
            require(
                value["task_id"] == task_id
                and value["state"] == "SUCCEEDED"
                and value["runtime"].get("publication") == "COMMITTED",
                "INVALID_BINDING",
                "Candidate must be a published task workflow",
            )
            output = value["runtime"]["publication_receipt"]["outputs"].get(selection["output"])
            for part in selection.get("path", []):
                require(
                    isinstance(output, list if isinstance(part, int) else dict),
                    "INVALID_BINDING",
                    "Workflow output path is invalid",
                )
                try:
                    output = output[part]
                except (IndexError, KeyError):
                    require(False, "INVALID_BINDING", "Workflow output path is invalid")
            require(
                isinstance(output, dict) and "asset" in output,
                "INVALID_BINDING",
                "Selected workflow output is not an asset",
            )
            ref = output["asset"]
            asset = self.data_call(
                actor, project, "GET", f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
            )
            require(
                asset["ref"] == ref
                and asset["manifest"]["source"]["task_ref"]
                == {"task_id": task_id, "spec_revision": request["expected_spec_revision"]},
                "INVALID_BINDING",
                "Candidate belongs to another task revision",
            )
            if all(item["ref"] != ref for item in artifacts):
                artifacts.append({"ref": ref, "manifest": asset["manifest"]})

        def operation(uow):
            task = uow.tasks.get(project, task_id, lock=True)
            task.admissible(request["expected_spec_revision"])
            acceptance_spec = uow.tasks.spec(task_id, task.spec_revision).get("acceptance_spec")
            exact_requirements = exact_output_requirements(acceptance_spec or {})
            require(
                bool(exact_requirements) and bool(canonical_bindings),
                "PRECONDITION_UNRESOLVED",
                "Exact output roles and exact deliverable bindings are required",
            )
            require(
                all("output_id" not in item for item in acceptance_spec["required_outputs"]),
                "PRECONDITION_UNRESOLVED",
                "Legacy output identities are not executable",
            )
            expected_by_role = {item["role_id"]: item for item in exact_requirements}
            require(
                set(binding_facts) <= set(expected_by_role),
                "INVALID_BINDING",
                "Deliverable contains an unknown output role",
            )
            for role_id, facts in binding_facts.items():
                require_expected(expected_by_role[role_id]["expected"], facts)
            identity_mode = "EXACT"
            fingerprint = exact_deliverable_digest(canonical_bindings)
            for previous in uow.science.candidates(task_id):
                if (
                    previous["spec_revision"] == task.spec_revision
                    and previous["digest"] == fingerprint
                ):
                    return previous
            record = {
                "deliverable_id": uow.new_id(),
                "project_id": project,
                "task_id": task_id,
                "spec_revision": task.spec_revision,
                "digest": fingerprint,
                "identity_mode": identity_mode,
                "payload": {
                    "artifacts": artifacts,
                    "executions": sorted(request["executions"]),
                    "workflow_outputs": request.get("workflow_outputs", []),
                    "role_bindings": canonical_bindings,
                    "identity_mode": identity_mode,
                },
            }
            uow.science.add_candidate(record)
            return record

        return self.control._write(
            actor, project, "candidate", key, request | {"task_id": task_id}, "EDITOR", operation
        )

    def candidates(self, actor, project, task_id):
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            uow.tasks.get(project, task_id)
            return uow.science.candidates(task_id)

    @staticmethod
    def check_receipt(project, task_id, record):
        return {
            "acceptance_id": record["acceptance_id"],
            "status": record["status"],
            "status_url": f"/api/v1/projects/{project}/tasks/{task_id}/acceptance/"
            + record["acceptance_id"],
        }

    def accept(self, actor, project, task_id, key, request, *, agent_context=None):
        self.authorize(actor, project, "EDITOR")
        with self.store.transaction() as uow:
            task = uow.tasks.get(project, task_id)
            spec = uow.tasks.spec(task_id, task.spec_revision).get("acceptance_spec")
        require(
            spec is not None,
            "PRECONDITION_UNRESOLVED",
            "Task has no structured acceptance specification",
        )
        require(
            spec["profile"] == "open-science-v1" or request.get("analysis_registration_id") is None,
            "INVALID_BINDING",
            "P3 acceptance does not use an analysis registration",
        )
        binding = (
            self.data_call(actor, project, "POST", "/resolve", body=spec["input"])
            if spec["profile"] == "p3-timeseries-v1"
            else None
        )

        def operation(uow):
            current = uow.tasks.get(project, task_id, lock=True)
            require(
                current.spec_revision == request["expected_spec_revision"] == task.spec_revision,
                "REVISION_CONFLICT",
                "Task requirements changed",
            )
            candidate = uow.science.candidate(project, request["deliverable_id"])
            require(
                candidate["task_id"] == task_id
                and candidate["spec_revision"] == current.spec_revision,
                "INVALID_BINDING",
                "Candidate task revision differs",
            )
            previous = [
                value
                for value in uow.science.checks(task_id)
                if value["deliverable_id"] == candidate["deliverable_id"]
            ]
            selected_registration_id = request.get("analysis_registration_id")
            retry = request.get("retry_of")
            if retry:
                parent = next(
                    (value for value in previous if value["acceptance_id"] == retry), None
                )
                require(
                    parent is not None and parent["status"] == "ERROR",
                    "PRECONDITION_UNRESOLVED",
                    "Only an ERROR check can be explicitly retried",
                )
                require(
                    parent.get("analysis_registration_id") == selected_registration_id,
                    "INVALID_BINDING",
                    "Acceptance retry must keep the frozen analysis registration",
                )
                repeated = next((value for value in previous if value["retry_of"] == retry), None)
            else:
                initial = [value for value in previous if value["retry_of"] is None]
                require(
                    not any(value["status"] == "ERROR" for value in initial),
                    "PRECONDITION_UNRESOLVED",
                    "Checker ERROR requires explicit retry_of",
                )
                repeated = next(
                    (
                        value
                        for value in initial
                        if value["status"] != "STALE"
                        and value.get("analysis_registration_id") == selected_registration_id
                    ),
                    None,
                )
            if repeated:
                return self.check_receipt(project, task_id, repeated)
            current.admissible(task.spec_revision)
            if spec["profile"] == "open-science-v1":
                exact_requirements = [
                    value for value in spec["required_outputs"] if "role_id" in value
                ]
                identity_mode = candidate["identity_mode"]
                if spec["delivery_class"] == "VERIFIED":
                    require(
                        identity_mode == "EXACT" and exact_requirements,
                        "PRECONDITION_UNRESOLVED",
                        "Verified delivery requires exact output role identity",
                    )
                    require(
                        selected_registration_id is not None,
                        "PRECONDITION_UNRESOLVED",
                        "Verified delivery requires an explicit analysis registration",
                    )
                    bound = {
                        value["role_id"] for value in candidate["payload"].get("role_bindings", [])
                    }
                    require(
                        all(
                            not value["necessary"] or value["role_id"] in bound
                            for value in exact_requirements
                        ),
                        "PRECONDITION_UNRESOLVED",
                        "Verified delivery is missing a necessary output role",
                    )
                from platform_core.application.checker_runs import admit as admit_checker_run
                from platform_core.application.checker_runs import request_for_run
                from platform_core.application.checker_runs import reserve as reserve_checker_runs
                from platform_core.application.open_acceptance import compile_coverage

                require(
                    self.checker_catalog is not None,
                    "PRECONDITION_UNRESOLVED",
                    "Checker catalog is not configured",
                )
                checker_catalog = self.checker_catalog
                plan, evidence = compile_coverage(
                    spec,
                    candidate,
                    uow.research.registrations(task_id),
                    uow.research.claims(task_id),
                    uow.research.policy_decisions(task_id),
                    uow.research.provenance(task_id),
                    uow.research.analyses(task_id),
                    selected_registration_id,
                    checker_catalog,
                )
                require(
                    0 < len(plan["checker_runs"]) <= spec["max_check_executions"],
                    "PRECONDITION_UNRESOLVED",
                    "Coverage requires more checker executions than allowed",
                )
                action_request = None
            else:
                from platform_core.application.catalog import CHECK_TARGET

                target = CHECK_TARGET
                action_request = {
                    "project_id": project,
                    "task_id": task_id,
                    "expected_spec_revision": current.spec_revision,
                    "target": target,
                    "resources": [spec["input"]["asset"]],
                    "parameters": spec["parameters"],
                    "kind": "CHECK",
                    "acceptance_spec": spec,
                    "binding": binding,
                    "artifacts": candidate["payload"]["artifacts"],
                }

            acceptance_id = uow.new_id()
            action = (
                self.control.admit(uow, actor, project, action_request, agent_context=agent_context)
                if spec["profile"] != "open-science-v1"
                else None
            )
            record = {
                "acceptance_id": acceptance_id,
                "project_id": project,
                "task_id": task_id,
                "spec_revision": current.spec_revision,
                "deliverable_id": candidate["deliverable_id"],
                "action_id": action["action_id"] if action else None,
                "analysis_registration_id": selected_registration_id,
                "retry_of": retry,
                "status": "QUEUED",
                "report": None,
                "report_digest": None,
            }
            uow.science.add_check(record)
            if spec["profile"] == "open-science-v1":
                from platform_core.application.control import digest

                plan_id = uow.new_id()
                uow.science.add_plan(
                    {
                        "plan_id": plan_id,
                        "acceptance_id": acceptance_id,
                        "project_id": project,
                        "task_id": task_id,
                        "revision": 1,
                        "delivery_class": spec["delivery_class"],
                        "source_digest": plan["source_digest"],
                        "plan": plan,
                        "plan_digest": plan["plan_digest"],
                        "status": "ACTIVE",
                        "created_at": uow.now(),
                    },
                    [
                        {
                            "plan_id": plan_id,
                            "item_id": item["item_id"],
                            "ordinal": item["ordinal"],
                            "requirement_id": item["requirement_id"],
                            "check_type": item["check_type"],
                            "necessary": item["necessary"],
                            "automatic": item["automatic"],
                            "status": item.get(
                                "initial_status",
                                "PLANNED" if item["automatic"] else "REVIEW_REQUIRED",
                            ),
                            "result": None,
                            "coverage_status": item.get("coverage_status", "COVERED"),
                            "capability_id": item.get("capability_id"),
                            "capability_version": item.get("capability_version"),
                            "capability_digest": item.get("capability_digest"),
                            "check_run_id": None,
                        }
                        for item in plan["items"]
                    ],
                    [
                        {
                            "acceptance_id": acceptance_id,
                            "evidence_digest": digest(ref),
                            "project_id": ref["project_id"],
                            "document_id": ref["document_id"],
                            "version_id": ref["version_id"],
                            "evidence_ref": ref,
                        }
                        for ref in evidence
                    ],
                )
                parent_runs = uow.science.check_runs(retry) if retry else []
                if retry:
                    parent_plan = uow.science.plan(retry)
                    require(
                        parent_plan is not None
                        and parent_plan["plan_digest"] == plan["plan_digest"],
                        "INVALID_BINDING",
                        "Acceptance retry requires the identical frozen plan and catalogue",
                    )
                prior_by_capability = {row["capability_id"]: row for row in parent_runs}
                run_rows = []
                for frozen in plan["checker_runs"]:
                    parent_run = prior_by_capability.get(frozen["capability_id"])
                    reused = parent_run is not None and parent_run["status"] in {"PASS", "REUSED"}
                    run_rows.append(
                        {
                            "run_id": uow.new_id(),
                            "acceptance_id": acceptance_id,
                            **frozen,
                            "plan_digest": plan["plan_digest"],
                            "action_id": None,
                            "status": "REUSED" if reused else "PLANNED",
                            "generation": 0,
                            "retry_of_run_id": parent_run["run_id"] if parent_run else None,
                            "report": parent_run["report"] if reused else None,
                            "report_digest": parent_run["report_digest"] if reused else None,
                        }
                    )
                planned = [row for row in run_rows if row["status"] == "PLANNED"]
                previous_started = sum(
                    row["action_id"] is not None
                    for value in previous
                    for row in uow.science.check_runs(value["acceptance_id"])
                )
                require(
                    previous_started + len(planned) <= spec["max_check_executions"],
                    "BUDGET_EXHAUSTED",
                    "Acceptance check execution limit reached",
                )
                uow.science.add_check_runs(run_rows)
                reserve_checker_runs(uow, current, acceptance_id, actor, plan, len(planned))
                if retry:
                    for row in run_rows:
                        if row["status"] == "REUSED" and row["report"]:
                            uow.science.apply_item_results(plan_id, row["report"]["items"])
                completed = {
                    row["capability_id"] for row in run_rows if row["status"] in {"PASS", "REUSED"}
                }
                first = next(row for row in planned if set(row["depends_on"]).issubset(completed))
                envelope = uow.science.check_envelope(acceptance_id)
                primary_action_id = admit_checker_run(
                    uow,
                    current,
                    envelope,
                    first,
                    request_for_run(
                        project,
                        task_id,
                        current.spec_revision,
                        spec,
                        plan,
                        first,
                        candidate,
                        selected_registration_id,
                    ),
                )
                uow.science.set_primary_check_action(acceptance_id, primary_action_id)
                record["action_id"] = primary_action_id
            return self.check_receipt(project, task_id, record)

        return self.control._write(
            actor, project, "acceptance", key, request | {"task_id": task_id}, "EDITOR", operation
        )

    def checks(self, actor, project, task_id, id=None):
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            task = uow.tasks.get(project, task_id)
            records = [uow.science.check(project, id)] if id else uow.science.checks(task_id)
            require(
                all(value["task_id"] == task_id for value in records),
                "RESOURCE_NOT_FOUND",
                "Acceptance not found",
                404,
            )
            completion = uow.science.completion(task_id)
            if completion:
                completion["completed_at"] = completion["completed_at"].isoformat()
                validity = uow.science.validity(task_id)
                completion["validity"] = validity["validity"] if validity else "CURRENT"
                completion["validity_revision"] = validity["revision"] if validity else 1
                completion["validity_reasons"] = validity["reasons"] if validity else []
            return [
                {
                    key: item
                    for key, item in value.items()
                    if key not in {"owner", "generation", "lease_until", "attempts"}
                }
                | {
                    "stale": value["spec_revision"] != task.spec_revision,
                    "completion": completion,
                    "coverage_plan": (
                        (
                            lambda plan: (
                                None
                                if plan is None
                                else {
                                    **plan["plan"],
                                    "status": plan["status"],
                                    "items": plan["items"],
                                }
                            )
                        )(uow.science.plan(value["acceptance_id"]))
                    ),
                    "human_reviews": [
                        row | {"created_at": row["created_at"].isoformat()}
                        for row in uow.science.reviews(value["acceptance_id"])
                    ],
                    "check_runs": [
                        {
                            key: row[key]
                            for key in (
                                "run_id",
                                "run_key",
                                "capability_id",
                                "capability_version",
                                "capability_digest",
                                "action_id",
                                "status",
                                "retry_of_run_id",
                                "report_digest",
                            )
                        }
                        for row in uow.science.check_runs(value["acceptance_id"])
                    ],
                }
                for value in records
            ]

    def review(self, actor, project, task_id, acceptance_id, key, request):
        from dataclasses import replace

        from platform_core.application.control import digest
        from platform_core.application.open_acceptance import compile_coverage, outcome

        def operation(uow):
            task = uow.tasks.get(project, task_id, lock=True)
            require(
                task.spec_revision == request["expected_spec_revision"],
                "REVISION_CONFLICT",
                "Task requirements changed",
            )
            record = uow.science.check(project, acceptance_id)
            require(record["task_id"] == task_id, "RESOURCE_NOT_FOUND", "Acceptance not found", 404)
            require(
                record["status"] == "REVIEW_REQUIRED",
                "PRECONDITION_UNRESOLVED",
                "Automated coverage must finish before human review",
            )
            plan = uow.science.plan(acceptance_id)
            require(
                plan is not None and plan["status"] == "ACTIVE",
                "PRECONDITION_UNRESOLVED",
                "Coverage plan is unavailable",
            )
            require(
                plan["revision"] == request["coverage_plan_revision"],
                "REVISION_CONFLICT",
                "Coverage plan changed",
            )
            spec = uow.tasks.spec(task_id, record["spec_revision"])["acceptance_spec"]
            require(
                spec["profile"] == "open-science-v1" and spec["allow_manual_review"],
                "SCOPE_DENIED",
                "Manual review is not allowed",
                403,
            )
            candidate = uow.science.candidate(project, record["deliverable_id"])
            current_plan, _ = compile_coverage(
                spec,
                candidate,
                uow.research.registrations(task_id),
                uow.research.claims(task_id),
                uow.research.policy_decisions(task_id),
                uow.research.provenance(task_id),
                uow.research.analyses(task_id),
            )
            require(
                current_plan["source_digest"] == plan["source_digest"],
                "REVISION_CONFLICT",
                "Coverage inputs changed after the plan was frozen",
            )
            item = next(
                (value for value in plan["items"] if value["item_id"] == request["item_id"]), None
            )
            require(
                item is not None
                and item["check_type"] == "HUMAN_REVIEW"
                and item["status"] == "REVIEW_REQUIRED",
                "PRECONDITION_UNRESOLVED",
                "Only a pending human-review item can be decided",
            )
            review = {
                "review_id": uow.new_id(),
                "project_id": project,
                "task_id": task_id,
                "acceptance_id": acceptance_id,
                "plan_id": plan["plan_id"],
                "plan_revision": plan["revision"],
                "item_id": item["item_id"],
                "reviewer_id": actor.principal_id,
                "qualification": request["qualification"],
                "criteria": request["criteria"],
                "decision": request["decision"],
                "reason": request["reason"],
                "created_at": uow.now(),
            }
            uow.science.add_review(review)
            refreshed = uow.science.plan(acceptance_id)
            status, limitations = outcome(refreshed["delivery_class"], refreshed["items"])
            report = (record["report"] or {}) | {
                "status": status,
                "delivery_class": refreshed["delivery_class"],
                "limitations": limitations,
                "coverage_plan_revision": refreshed["revision"],
            }
            uow.science.update_check(acceptance_id, status, report, digest(report))
            if (
                status == "PASS"
                and task.lifecycle == "OPEN"
                and task.control == "ACTIVE"
                and task.spec_revision == record["spec_revision"]
                and not uow.actions.active(task_id)
            ):
                completed_record = record | {
                    "status": status,
                    "report": report,
                    "report_digest": digest(report),
                }
                uow.science.complete(task, completed_record)
                uow.science.set_completion_validity(task_id, "CURRENT")
                task = replace(
                    task, lifecycle="COMPLETED", control_version=task.control_version + 1
                )
                uow.tasks.save(task)
                ControlService._event(uow, task, "TASK_COMPLETED")
            return review | {
                "created_at": review["created_at"].isoformat(),
                "acceptance_status": status,
            }

        return self.control._write(
            actor,
            project,
            "acceptance_review",
            key,
            request | {"task_id": task_id, "acceptance_id": acceptance_id},
            "OWNER",
            operation,
        )
