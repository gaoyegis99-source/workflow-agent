"""Workflow input materialization and submission use cases."""

from platform_core.application.control import digest
from platform_core.application.ports import Record
from platform_core.application.workflow_support import WorkflowFlow
from platform_core.domain.control import Identity, require


class WorkflowSubmissionFlow(WorkflowFlow):
    def _replay(
        self, actor, project, key, request, *, agent_context=None, operation="submit_workflow"
    ):
        self.control.validate_key(key)
        with self.store.transaction() as uow:
            if actor.is_service_continuation and agent_context is not None:
                uow.access.authorize_principal(actor.principal_id, project, "EDITOR")
            else:
                uow.access.authorize(actor, project, "EDITOR")
            result = uow.delivery.replay(actor, project, operation, key)
            if result is None:
                return None
            action_id = result.get("action_id") or result.get("command", {}).get("action_id")
            require(action_id is not None, "INVALID_BINDING", "Idempotent action is missing", 409)
            action = uow.actions.get(action_id)
            require(
                digest(action["request"].get("client_request")) == digest(request),
                "IDEMPOTENCY_CONFLICT",
                "Request key has different content",
            )
            return result

    def _resolve_inputs(self, actor, project, schemas, values):
        require(
            isinstance(values, dict) and set(values) <= set(schemas),
            "INVALID_BINDING",
            "Workflow inputs do not match the frozen package",
            422,
        )
        resources = []

        def resolve(schema, value, path):
            compound = (
                isinstance(schema, dict)
                and isinstance(schema.get("type"), str)
                and schema.get("type") in {"array", "record"}
            )
            wrapper = schema if isinstance(schema, dict) and not compound else {}
            if value is None and "default" in wrapper:
                value = wrapper["default"]
            schema = schema if compound else wrapper.get("type", schema)
            if isinstance(schema, list):
                if value is None and "null" in schema:
                    return None
                choices = [item for item in schema if item != "null"]
                require(len(choices) == 1, "INVALID_BINDING", "Invalid nullable input", 422)
                return resolve(choices[0], value, path)
            if schema == "File":
                require(
                    isinstance(value, dict) and "asset" in value,
                    "INVALID_BINDING",
                    "Workflow File inputs require an exact asset selection",
                    422,
                )
                require(
                    self.data is not None, "DEPENDENCY_UNAVAILABLE", "Data service unavailable", 503
                )
                binding = self.data.json(
                    "POST",
                    f"/internal/projects/{project}/resolve",
                    headers={"X-Actor-ID": actor.principal_id},
                    json=value,
                )
                resources.append(binding["selection"]["asset"])
                return {"class": "File", "binding": binding}
            if isinstance(schema, dict) and schema.get("type") == "array":
                require(isinstance(value, list), "INVALID_BINDING", "Array input required", 422)
                return [
                    resolve(schema["items"], item, path + [index])
                    for index, item in enumerate(value)
                ]
            if isinstance(schema, dict) and schema.get("type") == "record":
                require(isinstance(value, dict), "INVALID_BINDING", "Record input required", 422)
                fields = {field["name"]: field for field in schema["fields"]}
                require(set(value) <= set(fields), "INVALID_BINDING", "Unknown record field", 422)
                output = {}
                for name, field in fields.items():
                    if (
                        name in value
                        or "default" in field
                        or (isinstance(field["type"], list) and "null" in field["type"])
                    ):
                        output[name] = resolve(field, value.get(name), path + [name])
                    else:
                        require(False, "INVALID_BINDING", "Required record field is missing", 422)
                return output
            valid = {
                "boolean": type(value) is bool,
                "int": type(value) is int,
                "long": type(value) is int,
                "float": type(value) in {int, float},
                "double": type(value) in {int, float},
                "string": isinstance(value, str),
            }
            require(
                valid.get(schema, False), "INVALID_BINDING", "Workflow scalar type mismatch", 422
            )
            return value

        frozen = {}
        for name, schema in schemas.items():
            kind = schema.get("type") if isinstance(schema, dict) else None
            wrapper = (
                schema
                if isinstance(schema, dict)
                and not (isinstance(kind, str) and kind in {"array", "record"})
                else {}
            )
            optional = isinstance(wrapper.get("type", schema), list) and "null" in wrapper.get(
                "type", schema
            )
            if name in values or "default" in wrapper or optional:
                frozen[name] = resolve(schema, values.get(name), [name])
            else:
                require(False, "INVALID_BINDING", "Required workflow input is missing", 422)
        unique = {digest(resource): resource for resource in resources}
        return frozen, list(unique.values())

    def submit(
        self,
        actor: Identity,
        project: str,
        key: str,
        request: Record,
        *,
        agent_context: Record | None = None,
    ) -> Record:
        replay = self._replay(actor, project, key, request, agent_context=agent_context)
        if replay is not None:
            return replay
        with self.store.transaction() as uow:
            if actor.is_service_continuation and agent_context is not None:
                uow.access.authorize_principal(actor.principal_id, project, "EDITOR")
            else:
                uow.access.authorize(actor, project, "EDITOR")
            package = uow.workflows.package(project, request["target"]["package_ref"]["package_id"])
            require(
                self._package_matches(package, request["target"]["package_ref"]),
                "INVALID_BINDING",
                "Workflow package reference is not ready or exact",
                422,
            )
            schemas = package["compiled"]["inputs"]
        inputs, resources = self._resolve_inputs(actor, project, schemas, request["inputs"])
        return self.admit(
            actor,
            project,
            key,
            request | {"inputs": inputs, "resources": resources},
            idempotency_payload=request,
            agent_context=agent_context,
        )

    def submit_authoring_trial(
        self,
        actor: Identity,
        project: str,
        key: str,
        request: Record,
        authoring_trial: Record,
        *,
        client_request: Record | None = None,
    ) -> Record:
        idempotency_payload = client_request or request
        replay = self._replay(
            actor,
            project,
            key,
            idempotency_payload,
            operation="submit_workflow_trial",
        )
        if replay is not None:
            return replay
        with self.store.transaction() as uow:
            self._authorize_actor(uow, actor, project)
            package = uow.workflows.package(project, request["target"]["package_ref"]["package_id"])
            require(
                self._package_matches(package, request["target"]["package_ref"]),
                "INVALID_BINDING",
                "Workflow package reference is not ready or exact",
                422,
            )
            schemas = package["compiled"]["inputs"]
        inputs, resources = self._resolve_inputs(actor, project, schemas, request["inputs"])
        return self.admit(
            actor,
            project,
            key,
            request | {"inputs": inputs, "resources": resources},
            idempotency_payload=idempotency_payload,
            operation="submit_workflow_trial",
            authoring_trial=authoring_trial,
        )
