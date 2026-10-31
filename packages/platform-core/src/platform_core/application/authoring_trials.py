"""Authoring trial construction and trial read-model use cases."""

from platform_core.application.authoring_release_support import (
    AuthoringReleaseFlow,
)
from platform_core.application.authoring_release_support import (
    authorize as _authorize,
)
from platform_core.application.authoring_release_support import (
    candidate_view as _candidate_view,
)
from platform_core.application.authoring_release_support import (
    trial_view as _trial_view,
)
from platform_core.application.control import ControlService
from platform_core.application.ports import Record
from platform_core.domain.control import RuleViolation, require


class AuthoringTrialFlow(AuthoringReleaseFlow):
    def _trusted_output_schemas(self, source: Record) -> dict[str, set[str]]:
        require(
            self.descriptor is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Descriptor unavailable",
            503,
        )
        operators = {
            (item["operator_id"], item["version_id"]): item for item in self.descriptor["operators"]
        }
        documents = source["documents"]
        implementations = source["implementations"]
        cache = {}

        def schemas(reference):
            if reference in cache:
                return cache[reference]
            cache[reference] = {}
            document = documents[reference]
            values = {}
            for output, declaration in document.get("outputs", {}).items():
                binding = declaration.get("outputSource") if isinstance(declaration, dict) else None
                if not isinstance(binding, str) or "/" not in binding:
                    values[output] = set()
                    continue
                step_name, port = binding.split("/", 1)
                step = document.get("steps", {}).get(step_name, {})
                run = step.get("run")
                if run in documents:
                    values[output] = set(schemas(run).get(port, set()))
                    continue
                target = implementations.get(run, {})
                descriptor = operators.get((target.get("operator_id"), target.get("version_id")))
                port_descriptor = (descriptor or {}).get("output_ports", {}).get(port, {})
                values[output] = set(port_descriptor.get("schema_ids", []))
            cache[reference] = values
            return values

        return schemas(source["entrypoint"])

    def create_trial(self, actor, project, definition_id, key, request) -> Record:
        ControlService.validate_key(key)
        idempotency_payload = {"definition_id": definition_id, **request}
        workflows = self.workflows
        replay = workflows._replay(
            actor,
            project,
            key,
            idempotency_payload,
            operation="submit_workflow_trial",
        )
        if replay is not None:
            return replay
        with self.store.transaction() as uow:
            _authorize(uow, actor, project, "EDITOR")
            candidate = uow.authoring.candidate(project, definition_id, request["candidate_id"])
            package = uow.workflows.package(project, candidate["package_id"])
            view = _candidate_view(candidate, package, self.environment)
            require(view["current_valid"], "REVISION_CONFLICT", "Candidate is not ready", 409)
            trial_id = uow.new_id()
            source = package["request"]
        schemas = self._trusted_output_schemas(source)
        expectations = request.get("trial_expectations", [])
        for expectation in expectations:
            require(
                expectation["output"] in schemas
                and expectation["schema_id"] in schemas[expectation["output"]],
                "INVALID_BINDING",
                "Trial expectation is not a trusted final output schema",
                422,
            )
        workflow_request = {
            "task_id": request["task_id"],
            "expected_spec_revision": request["expected_spec_revision"],
            "target": {"kind": "WORKFLOW", "package_ref": view["package_ref"]},
            "inputs": request["inputs"],
            "max_node_executions": request["max_node_executions"],
        }
        return workflows.submit_authoring_trial(
            actor,
            project,
            key,
            workflow_request,
            {
                "trial_id": trial_id,
                "definition_id": definition_id,
                "candidate_id": candidate["candidate_id"],
                "source_digest": candidate["source_digest"],
                "context": candidate["context"],
                "context_digest": candidate["context_digest"],
                "expectations": expectations,
            },
            client_request=idempotency_payload,
        )

    def _execution_outcome(self, project: str, action: Record) -> str:
        execution_id = action.get("execution_id")
        if execution_id is None:
            return action.get("state", "QUEUED")
        if self.execution is None:
            return "UNKNOWN"
        try:
            return self.execution.json(
                "GET", f"/internal/projects/{project}/executions/{execution_id}"
            )["state"]
        except RuleViolation:
            return "UNKNOWN"

    def trial(self, actor, project, definition_id, trial_id):
        with self.store.transaction() as uow:
            _authorize(uow, actor, project)
            record = uow.authoring.trial(project, definition_id, trial_id)
            action = uow.actions.get(record["action_id"])
        return _trial_view(record, action, self._execution_outcome(project, action))

    def trials(self, actor, project, definition_id, after, limit):
        with self.store.transaction() as uow:
            _authorize(uow, actor, project)
            uow.authoring.definition(project, definition_id)
            records = uow.authoring.trials(project, definition_id, after, limit)
            pairs = [(record, uow.actions.get(record["action_id"])) for record in records]
        return [
            _trial_view(record, action, self._execution_outcome(project, action))
            for record, action in pairs
        ]
