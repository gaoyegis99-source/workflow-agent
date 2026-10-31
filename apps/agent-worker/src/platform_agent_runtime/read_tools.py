"""Production adapters for the P6.3 read-only ToolGateway."""

from __future__ import annotations

from datetime import date, datetime

from platform_core.application.catalog import operators
from platform_core.application.read_tools import ReadResult
from platform_core.domain.control import require


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _asset_matches(resource, asset_id, version_id=None):
    return (
        isinstance(resource, dict)
        and resource.get("asset_id") == asset_id
        and (version_id is None or resource.get("version_id") == version_id)
    )


def _asset_cursor(resource):
    return ":".join(
        (
            resource["asset_id"],
            resource["version_id"],
            resource["version_manifest_digest"],
        )
    )


class PlatformReadTools:
    def __init__(
        self,
        database,
        *,
        data=None,
        knowledge=None,
        execution=None,
        checkers,
        operator_catalog,
    ):
        self.database = database
        self.data = data
        self.knowledge = knowledge
        self.execution = execution
        self.checkers = checkers
        self.operator_catalog = operator_catalog

    def _resources(self, context):
        with self.database.transaction() as uow:
            now = uow.now()
            grants = uow.tasks.active_grants(context["project_id"], context["task_id"], now)
            unique = {
                _asset_cursor(resource): resource
                for grant in grants
                for resource in grant["resources"]
            }
            return [unique[cursor] for cursor in sorted(unique)]

    def authorize(self, tool_id, arguments, context):
        if tool_id in {"data.read_asset_manifest", "data.read_semantic_revision"}:
            require(
                any(
                    _asset_matches(value, arguments["asset_id"], arguments["version_id"])
                    for value in self._resources(context)
                ),
                "SCOPE_DENIED",
                "Asset is outside the Task grant",
                403,
            )
        if tool_id.startswith("execution."):
            with self.database.transaction() as uow:
                action = uow.actions.by_execution(context["project_id"], arguments["execution_id"])
                require(
                    action["task_id"] == context["task_id"],
                    "SCOPE_DENIED",
                    "Execution belongs to another Task",
                    403,
                )

    def execute(self, tool_id, arguments, context, tool_request_id):
        project, task_id = context["project_id"], context["task_id"]
        if tool_id == "task.read_current":
            with self.database.transaction() as uow:
                task = uow.tasks.get(project, task_id)
                budget = uow.budgets.get(task_id)
                return ReadResult(
                    _json(
                        {
                            "task_id": task.task_id,
                            "spec_revision": task.spec_revision,
                            "control_version": task.control_version,
                            "lifecycle": task.lifecycle,
                            "control": task.control,
                            "cleanup_status": task.cleanup_status,
                            "budget": budget.__dict__
                            if hasattr(budget, "__dict__")
                            else {
                                key: getattr(budget, key)
                                for key in (
                                    "execution_count",
                                    "max_in_flight",
                                    "used",
                                    "reserved",
                                    "in_flight",
                                )
                            },
                        }
                    )
                )
        if tool_id == "task.read_requirements":
            with self.database.transaction() as uow:
                task = uow.tasks.get(project, task_id)
                spec = uow.tasks.spec(task_id, task.spec_revision)
            return ReadResult(_json(spec), contains_untrusted_data=True)
        if tool_id == "plan.read_current":
            with self.database.transaction() as uow:
                run = uow.agent_runs.run_by_task(task_id, required=True)
                plan = uow.agent_runs.plan(run["agent_run_id"])
            return ReadResult(_json(plan or {"status": "NO_PLAN"}))
        if tool_id == "data.list_assets":
            require(self.data is not None, "DEPENDENCY_UNAVAILABLE", "Data is unavailable", 503)
            resources = self._resources(context)
            after, limit = arguments.get("after", ""), arguments.get("limit", 50)
            selected = [resource for resource in resources if _asset_cursor(resource) > after]
            page, remaining = selected[:limit], selected[limit:]
            items = []
            for resource in page:
                value = self.data.json(
                    "GET",
                    f"/internal/projects/{project}/assets/{resource['asset_id']}"
                    f"/versions/{resource['version_id']}",
                )
                require(
                    value.get("ref") == resource,
                    "INVALID_BINDING",
                    "Granted asset identity differs",
                    409,
                )
                items.append(value)
            return ReadResult(
                {
                    "items": items,
                    "next_cursor": _asset_cursor(page[-1]) if remaining and page else None,
                },
                contains_untrusted_data=True,
            )
        if tool_id in {"data.read_asset_manifest", "data.read_semantic_revision"}:
            require(self.data is not None, "DEPENDENCY_UNAVAILABLE", "Data is unavailable", 503)
            value = self.data.json(
                "GET",
                f"/internal/projects/{project}/assets/{arguments['asset_id']}/versions/{arguments['version_id']}",
            )
            require(
                value.get("ref") in self._resources(context),
                "INVALID_BINDING",
                "Granted asset identity differs",
                409,
            )
            if tool_id == "data.read_semantic_revision":
                revision = next(
                    (
                        item
                        for item in value.get("semantic_revisions", [])
                        if item.get("revision") == arguments["revision"]
                    ),
                    None,
                )
                require(
                    revision is not None, "RESOURCE_NOT_FOUND", "Semantic revision not found", 404
                )
                value = revision
            return ReadResult(
                value,
                refs=(
                    {
                        "kind": "ASSET",
                        "asset_id": arguments["asset_id"],
                        "version_id": arguments["version_id"],
                    },
                ),
                contains_untrusted_data=True,
            )
        if tool_id in {
            "knowledge.search",
            "knowledge.semantic_search",
            "knowledge.read_evidence",
        }:
            require(
                self.knowledge is not None,
                "DEPENDENCY_UNAVAILABLE",
                "Knowledge is unavailable",
                503,
            )
            resources = self._resources(context)
            if tool_id == "knowledge.search":
                result = self.knowledge.json(
                    "POST",
                    f"/internal/projects/{project}/knowledge/search-scoped",
                    json={
                        "query": arguments["query"],
                        "limit": arguments.get("limit", 20),
                        "sources": resources,
                    },
                )
                return ReadResult(result, contains_untrusted_data=True)
            if tool_id == "knowledge.semantic_search":
                result = self.knowledge.json(
                    "POST",
                    f"/internal/projects/{project}/knowledge/semantic-search-scoped",
                    json={
                        "query": arguments["query"],
                        "limit": arguments.get("limit", 20),
                        "index": {
                            "index_id": arguments["index_id"],
                            "profile_digest": arguments["profile_digest"],
                        },
                        "sources": resources,
                    },
                )
                # Candidate snippets are untrusted discovery content.  The
                # model must call knowledge.read_evidence before citing one.
                return ReadResult(result, contains_untrusted_data=True)
            version = self.knowledge.json(
                "GET",
                f"/internal/projects/{project}/knowledge/documents/{arguments['document_id']}/versions/{arguments['version_id']}",
            )
            source = version.get("source", {})
            require(
                source in resources,
                "SCOPE_DENIED",
                "Evidence source is outside the Task grant",
                403,
            )
            value = self.knowledge.json(
                "GET",
                f"/internal/projects/{project}/knowledge/documents/{arguments['document_id']}/versions/{arguments['version_id']}/units/{arguments['unit_id']}",
                headers={
                    "X-Actor-ID": context["principal_id"],
                    "X-Read-Receipt-ID": tool_request_id,
                },
            )
            return ReadResult(
                value,
                refs=(value["evidence_ref"], {"read_receipt_id": value["read_receipt_id"]}),
                contains_untrusted_data=True,
            )
        if tool_id == "capability.list_operators":
            return ReadResult({"items": operators(self.operator_catalog)})
        if tool_id == "capability.list_workflows":
            after, limit = arguments.get("after", ""), arguments.get("limit", 50)
            with self.database.transaction() as uow:
                rows = uow.workflows.packages(project, after, limit + 1)
            page = rows[:limit]
            return ReadResult(
                {
                    "items": [
                        {
                            "package_id": row["package_id"],
                            "status": row["status"],
                            "digest": row["digest"],
                            "name": row["request"].get("name"),
                        }
                        for row in page
                    ],
                    "next_cursor": page[-1]["package_id"] if len(rows) > limit else None,
                }
            )
        if tool_id == "capability.list_checkers":
            return ReadResult({"items": self.checkers["capabilities"]})
        if tool_id == "execution.read":
            require(
                self.execution is not None,
                "DEPENDENCY_UNAVAILABLE",
                "Execution is unavailable",
                503,
            )
            value = self.execution.json(
                "GET",
                f"/internal/projects/{project}/executions/{arguments['execution_id']}",
            )
            return ReadResult(
                value,
                refs=({"execution_id": arguments["execution_id"]},),
                contains_untrusted_data=True,
            )
        if tool_id == "execution.read_steps":
            with self.database.transaction() as uow:
                action = uow.actions.by_execution(project, arguments["execution_id"])
                rows = uow.workflows.nodes(
                    action["action_id"], arguments.get("after", ""), arguments.get("limit", 100)
                )
            return ReadResult({"items": _json(rows)}, contains_untrusted_data=True)
        if tool_id == "research.read":
            with self.database.transaction() as uow:
                value = {
                    "analysis_revisions": uow.research.analyses(task_id),
                    "registrations": uow.research.registrations(task_id),
                    "claims": uow.research.claims(task_id),
                    "provenance": uow.research.provenance(task_id),
                    "policy_decisions": uow.research.policy_decisions(task_id),
                }
            return ReadResult(_json(value), contains_untrusted_data=True)
        if tool_id == "acceptance.read":
            with self.database.transaction() as uow:
                value = {
                    "deliverables": uow.science.candidates(task_id),
                    "acceptances": uow.science.checks(task_id),
                    "completion": uow.science.completion(task_id),
                    "validity": uow.science.validity(task_id),
                }
            return ReadResult(_json(value), contains_untrusted_data=True)
        require(False, "UNSUPPORTED_TARGET", "Unknown read tool", 422)
