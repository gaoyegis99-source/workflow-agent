"""Production P6.4 action adapter using existing P3 application services."""

from __future__ import annotations

from platform_contracts.research import (
    AnalysisRevisionCreate,
    ClaimRevisionCreate,
    RegisterAnalysis,
)
from platform_contracts.science import (
    AcceptanceCreate,
    DeliverableCreate,
    ExecutionCreate,
    TableExecutionCreate,
)
from platform_contracts.workflows import WorkflowExecutionCreate
from platform_core.application.action_tools import ActionResult
from platform_core.application.catalog import configured_catalog, operators, resolve, target
from platform_core.application.research import Research
from platform_core.application.science import Science
from platform_core.application.workflows import Workflows
from platform_core.domain.control import Identity, require


class PlatformActionTools:
    def __init__(self, database, *, data, execution, knowledge=None, operator_catalog=None):
        self.database = database
        self.data = data
        self.knowledge = knowledge
        self.operator_catalog = operator_catalog or configured_catalog()
        self.science = Science(database, data, execution, operator_catalog=self.operator_catalog)
        self.workflows = Workflows(
            database, data, execution, operator_catalog=self.operator_catalog
        )
        self.research = Research(database)

    def _task_spec(self, context):
        with self.database.transaction() as uow:
            task = uow.tasks.get(context["project_id"], context["task_id"], lock=True)
            uow.access.authorize_principal(context["principal_id"], context["project_id"], "EDITOR")
            require(
                task.lifecycle == "OPEN"
                and task.control == "ACTIVE"
                and task.spec_revision == context["task_spec_revision"],
                "STALE_FRAME",
                "Task changed before the Agent action",
            )
            return uow.tasks.spec(task.task_id, task.spec_revision)

    def _p3_operator(self, operator_id, version_id="1.0.0"):
        require(
            operator_id in {"timeseries-completeness", "statistics-linear-trend"},
            "UNSUPPORTED_TARGET",
            "P6.4 only enables the two trusted P3 operators",
            422,
        )
        selected = next(
            (
                value
                for value in operators(self.operator_catalog)
                if value["operator_id"] == operator_id and value["version_id"] == version_id
            ),
            None,
        )
        require(selected is not None, "UNSUPPORTED_TARGET", "Operator is unavailable", 422)
        resolve(target(selected), self.operator_catalog)
        return selected

    def execute(self, tool_id, arguments, context, tool_request_id):
        actor = Identity(context["principal_id"], None)
        project, task_id = context["project_id"], context["task_id"]
        agent_context = {
            "frame_id": context["frame_id"],
            "tool_request_id": tool_request_id,
            "agent_run_id": context["agent_run_id"],
        }
        if tool_id == "execution.submit_workflow":
            spec = self._task_spec(context)
            frozen = spec.get("agent_workflow")
            require(
                isinstance(frozen, dict),
                "PRECONDITION_UNRESOLVED",
                "Task has no frozen Agent workflow intent",
            )
            body = WorkflowExecutionCreate.model_validate(
                {
                    "task_id": task_id,
                    "expected_spec_revision": context["task_spec_revision"],
                    **frozen,
                }
            ).model_dump(mode="json")
            require(
                body["target"]["package_ref"]["package_id"] == arguments["package_id"],
                "INVALID_BINDING",
                "Workflow selection differs from the frozen Task intent",
                422,
            )
            receipt = self.workflows.submit(
                actor,
                project,
                tool_request_id,
                body,
                agent_context=agent_context,
            )
            return ActionResult(
                value=receipt,
                operation="SUBMIT_WORKFLOW",
                business_id=receipt["action_id"],
                action_id=receipt["action_id"],
                command_id=receipt["command_id"],
                refs=(
                    {
                        "kind": "ACTION",
                        "action_id": receipt["action_id"],
                        "command_id": receipt["command_id"],
                    },
                ),
            )

        if tool_id == "research.create_analysis":
            spec = self._task_spec(context)
            frozen = spec.get("agent_analysis")
            require(
                isinstance(frozen, dict),
                "PRECONDITION_UNRESOLVED",
                "Task has no frozen Agent analysis template",
            )
            body = AnalysisRevisionCreate.model_validate(
                {"expected_spec_revision": context["task_spec_revision"], **frozen}
            ).model_dump(mode="json")
            resolved = self._resolve_analysis_sources(actor, project, body)
            value = self.research.create_analysis(
                actor, project, task_id, tool_request_id, body, resolved
            )
            return ActionResult(
                value=value,
                operation="CREATE_ANALYSIS",
                business_id=value["revision_id"],
                refs=(
                    {
                        "kind": "ANALYSIS_REVISION",
                        "revision_id": value["revision_id"],
                    },
                ),
            )

        if tool_id == "research.register_analysis":
            self._task_spec(context)
            body = RegisterAnalysis.model_validate(
                {"expected_spec_revision": context["task_spec_revision"]}
            ).model_dump(mode="json")
            value = self.research.register(
                actor,
                project,
                task_id,
                arguments["revision_id"],
                tool_request_id,
                body,
            )
            return ActionResult(
                value=value,
                operation="REGISTER_ANALYSIS",
                business_id=value["registration_id"],
                refs=(
                    {
                        "kind": "ANALYSIS_REGISTRATION",
                        "registration_id": value["registration_id"],
                    },
                ),
            )

        if tool_id == "research.create_claim":
            spec = self._task_spec(context)
            frozen = spec.get("agent_claim")
            require(
                isinstance(frozen, dict),
                "PRECONDITION_UNRESOLVED",
                "Task has no frozen Agent claim template",
            )
            body = ClaimRevisionCreate.model_validate(
                {
                    "expected_spec_revision": context["task_spec_revision"],
                    "analysis_registration_id": arguments["registration_id"],
                    **frozen,
                }
            ).model_dump(mode="json")
            sources = [item["source"] for item in body["sources"]]
            resolved = [self._resolve_source(actor, project, source) for source in sources]
            value = self.research.create_claim(
                actor, project, task_id, tool_request_id, body, resolved
            )
            return ActionResult(
                value=value,
                operation="CREATE_CLAIM",
                business_id=value["claim_revision_id"],
                refs=(
                    {
                        "kind": "CLAIM_REVISION",
                        "claim_revision_id": value["claim_revision_id"],
                    },
                ),
            )
        if tool_id == "execution.submit_operator":
            spec = self._task_spec(context)
            tool_version = context.get("tool_version", "1.0.0")
            require(
                tool_version in {"1.0.0", "2.0.0"},
                "UNSUPPORTED_TARGET",
                "Unsupported submit operator tool version",
                422,
            )
            intent = next(
                (
                    value
                    for value in spec.get("agent_operator_intents", [])
                    if value.get("operator_id") == arguments["operator_id"]
                    and value.get("version_id") == arguments.get("version_id")
                ),
                None,
            )
            if intent is not None:
                require(
                    tool_version == "2.0.0",
                    "UNSUPPORTED_TARGET",
                    "Curated operator intents require submit_operator v2",
                    422,
                )
                selected = next(
                    (
                        value
                        for value in operators(self.operator_catalog)
                        if value["operator_id"] == intent["operator_id"]
                        and value["version_id"] == intent["version_id"]
                    ),
                    None,
                )
                require(selected is not None, "UNSUPPORTED_TARGET", "Operator is unavailable", 422)
                body = TableExecutionCreate.model_validate(
                    {
                        "task_id": task_id,
                        "expected_spec_revision": context["task_spec_revision"],
                        "target": target(selected),
                        "inputs": {"table": intent["input"]},
                        "parameters": intent["parameters"],
                    }
                ).model_dump(mode="json")
                receipt = self.science.submit(
                    actor,
                    project,
                    tool_request_id,
                    body,
                    agent_context=agent_context,
                )
                return ActionResult(
                    value=receipt,
                    operation="SUBMIT_OPERATOR",
                    business_id=receipt["action_id"],
                    action_id=receipt["action_id"],
                    command_id=receipt["command_id"],
                    refs=(
                        {
                            "kind": "ACTION",
                            "action_id": receipt["action_id"],
                            "command_id": receipt["command_id"],
                        },
                    ),
                )
            acceptance = spec.get("acceptance_spec")
            require(
                acceptance is not None and acceptance.get("profile") == "p3-timeseries-v1",
                "PRECONDITION_UNRESOLVED",
                "P6.4 operator actions require a P3 time-series acceptance specification",
            )
            operator = self._p3_operator(
                arguments["operator_id"],
                arguments.get("version_id", "1.0.0"),
            )
            require(
                operator["operator_id"] in acceptance["required_outputs"],
                "SCOPE_DENIED",
                "Operator is not required by the current Task specification",
                403,
            )
            body = ExecutionCreate.model_validate(
                {
                    "task_id": task_id,
                    "expected_spec_revision": context["task_spec_revision"],
                    "target": target(operator),
                    "inputs": {"observations": acceptance["input"]},
                    "parameters": acceptance["parameters"],
                }
            ).model_dump(mode="json")
            receipt = self.science.submit(
                actor,
                project,
                tool_request_id,
                body,
                agent_context=agent_context,
            )
            return ActionResult(
                value=receipt,
                operation="SUBMIT_OPERATOR",
                business_id=receipt["action_id"],
                action_id=receipt["action_id"],
                command_id=receipt["command_id"],
                refs=(
                    {
                        "kind": "ACTION",
                        "action_id": receipt["action_id"],
                        "command_id": receipt["command_id"],
                    },
                ),
            )

        if tool_id == "deliverable.register":
            self._task_spec(context)
            body = DeliverableCreate.model_validate(
                {
                    "expected_spec_revision": context["task_spec_revision"],
                    "executions": arguments["execution_ids"],
                }
            ).model_dump(mode="json")
            value = self.science.candidate(actor, project, task_id, tool_request_id, body)
            return ActionResult(
                value=value,
                operation="REGISTER_DELIVERABLE",
                business_id=value["deliverable_id"],
                refs=({"kind": "DELIVERABLE", "deliverable_id": value["deliverable_id"]},),
            )

        if tool_id == "deliverable.register_exact":
            spec = self._task_spec(context)
            acceptance = spec.get("acceptance_spec")
            require(
                acceptance is not None and acceptance.get("profile") == "open-science-v1",
                "PRECONDITION_UNRESOLVED",
                "Exact role registration requires an open-science Task specification",
            )
            expected = {
                item["role_id"]: item
                for item in acceptance.get("required_outputs", [])
                if "role_id" in item
            }
            require(
                arguments["role_id"] in expected,
                "INVALID_BINDING",
                "Output role is not frozen in the Task specification",
                422,
            )
            requirement = expected[arguments["role_id"]]["expected"]
            require(
                requirement["output_port"] == arguments["output_port"],
                "INVALID_BINDING",
                "Output port differs from the frozen role contract",
                422,
            )
            binding = self.science.operator_role_binding(
                actor,
                project,
                task_id,
                context["task_spec_revision"],
                arguments["role_id"],
                arguments["execution_id"],
                arguments["output_port"],
            )
            body = DeliverableCreate.model_validate(
                {
                    "expected_spec_revision": context["task_spec_revision"],
                    "role_bindings": [binding],
                }
            ).model_dump(mode="json")
            value = self.science.candidate(actor, project, task_id, tool_request_id, body)
            return ActionResult(
                value=value,
                operation="REGISTER_DELIVERABLE",
                business_id=value["deliverable_id"],
                refs=({"kind": "DELIVERABLE", "deliverable_id": value["deliverable_id"]},),
            )

        if tool_id == "acceptance.request":
            self._task_spec(context)
            body = AcceptanceCreate.model_validate(
                {
                    "expected_spec_revision": context["task_spec_revision"],
                    "deliverable_id": arguments["deliverable_id"],
                    **({"retry_of": arguments["retry_of"]} if arguments.get("retry_of") else {}),
                    **(
                        {"analysis_registration_id": arguments["analysis_registration_id"]}
                        if arguments.get("analysis_registration_id")
                        else {}
                    ),
                }
            ).model_dump(mode="json")
            receipt = self.science.accept(
                actor,
                project,
                task_id,
                tool_request_id,
                body,
                agent_context=agent_context,
            )
            with self.database.transaction() as uow:
                check = uow.science.check(project, receipt["acceptance_id"])
            return ActionResult(
                value=receipt,
                operation="REQUEST_ACCEPTANCE",
                business_id=receipt["acceptance_id"],
                action_id=check["action_id"],
                refs=(
                    {
                        "kind": "ACCEPTANCE",
                        "acceptance_id": receipt["acceptance_id"],
                        "action_id": check["action_id"],
                    },
                ),
            )

        require(False, "UNSUPPORTED_TARGET", "Unknown controlled action tool", 422)

    def _resolve_source(self, actor, project, source):
        kind = source["kind"]
        if kind == "ASSET":
            require(self.data is not None, "DEPENDENCY_UNAVAILABLE", "Data is unavailable", 503)
            ref = source["ref"]
            value = self.data.json(
                "GET",
                f"/internal/projects/{project}/assets/{ref['asset_id']}/versions/{ref['version_id']}",
            )
            require(value.get("ref") == ref, "INVALID_BINDING", "Asset identity differs", 409)
            return {"verified": True}
        if kind == "EVIDENCE":
            require(
                self.knowledge is not None,
                "DEPENDENCY_UNAVAILABLE",
                "Knowledge is unavailable",
                503,
            )
            ref = source["ref"]
            value = self.knowledge.json(
                "GET",
                f"/internal/projects/{project}/knowledge/documents/{ref['document_id']}"
                f"/versions/{ref['version_id']}/units/{ref['unit_id']}",
                headers={"X-Actor-ID": actor.principal_id},
            )
            require(
                value.get("evidence_ref") == ref and value.get("availability") == "ACTIVE",
                "INVALID_BINDING",
                "Evidence identity or lifecycle differs",
                409,
            )
            return {
                "verified": True,
                "read_receipt_id": value["read_receipt_id"],
                "lifecycle_revision": value["lifecycle_revision"],
                "availability": value["availability"],
                "epistemic_status": value["epistemic_status"],
                "review_state": value["review_state"],
            }
        if kind == "METHOD":
            return {"verified": True}
        if kind == "EXECUTION":
            return None
        require(False, "INVALID_BINDING", "Unsupported research source", 409)

    def _resolve_analysis_sources(self, actor, project, body):
        sources = [item["source"] for item in body["input_roles"]]
        sources.extend({"kind": "METHOD", "ref": item} for item in body["methods"])
        sources.extend(body["observed_sources"])
        comparison = body.get("groundwater_comparison")
        for series in comparison["series"] if comparison else []:
            for name in ("elevation_conversion", "datum_conversion"):
                conversion = series.get(name)
                if conversion:
                    sources.append(conversion["source"])
        return [self._resolve_source(actor, project, source) for source in sources]
