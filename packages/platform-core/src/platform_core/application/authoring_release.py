"""Capability Studio candidate, trial and immutable release use cases."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from platform_core.application.authoring import environment_stale_reasons
from platform_core.application.catalog import resolve
from platform_core.application.control import ControlService, digest
from platform_core.application.ports import Identity, Record, Store
from platform_core.application.remote_bindings import freeze_remote_binding
from platform_core.application.workflows import PackageProcessor, Workflows, package_view
from platform_core.domain.authoring import canonical_digest
from platform_core.domain.control import RuleViolation, require

_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?$"
)
_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"}


def _authorize(uow, actor: Identity, project: str, role: str = "VIEWER") -> None:
    if actor.token_id is None:
        uow.access.authorize_principal(actor.principal_id, project, role)
    else:
        uow.access.authorize(actor, project, role)


def _candidate_view(record: Record, package: Record, environment: Record | None) -> Record:
    stale = (
        environment_stale_reasons(record["context"], environment)
        if environment is not None
        else ["VALIDATION_ENVIRONMENT_UNAVAILABLE"]
    )
    projected = package_view(package)
    return {
        "candidate_id": record["candidate_id"],
        "project_id": record["project_id"],
        "definition_id": record["definition_id"],
        "revision": record["revision"],
        "validation_id": record["validation_id"],
        "source_digest": record["source_digest"],
        "context_digest": record["context_digest"],
        "package_id": record["package_id"],
        "package_ref": projected["package_ref"],
        "status": projected["status"],
        "status_url": (
            f"/api/v1/projects/{record['project_id']}/workflow-definitions/"
            f"{record['definition_id']}/candidates/{record['candidate_id']}"
        ),
        "error": projected["error"],
        "created_by": record["created_by"],
        "created_at": record["created_at"].isoformat(),
        "current_valid": projected["status"] == "READY" and not stale,
        "stale_reasons": stale,
    }


def _trial_view(record: Record, action: Record, execution_outcome: str) -> Record:
    return {
        "project_id": record["project_id"],
        "definition_id": record["definition_id"],
        "trial_id": record["trial_id"],
        "candidate_id": record["candidate_id"],
        "task_id": record["task_id"],
        "action_id": record["action_id"],
        "execution_id": action.get("execution_id"),
        "execution_outcome": execution_outcome,
        "trial_case_digest": record["trial_case_digest"],
        "case_verdict": record["case_verdict"],
        "case_result": record["case_result"],
        "case_result_digest": record["case_result_digest"],
        "error_category": record["error_category"],
        "generation": record["generation"],
        "created_by": record["created_by"],
        "created_at": record["created_at"].isoformat(),
        "finished_at": record["finished_at"].isoformat() if record["finished_at"] else None,
    }


def semver_sort(label: str) -> list[str]:
    match = _SEMVER.fullmatch(label)
    require(match is not None and len(label) <= 64, "MALFORMED_REQUEST", "Invalid SemVer", 422)
    result = [f"{int(value):020d}" for value in match.group(1, 2, 3)]
    prerelease = match.group(4)
    if prerelease is None:
        return result + ["2:"]
    return result + [
        ("0:" + f"{int(value):020d}") if value.isdigit() else "1:" + value
        for value in prerelease.split(".")
    ]


class AuthoringReleaseService:
    def __init__(
        self,
        store: Store,
        data=None,
        execution=None,
        *,
        descriptor: Record | None = None,
        environment: Record | None = None,
        operator_catalog: Record | None = None,
    ):
        self.store = store
        self.data = data
        self.execution = execution
        self.descriptor = descriptor
        self.environment = environment
        self.operator_catalog = operator_catalog
        self.control = ControlService(store)

    def create_candidate(
        self,
        actor: Identity,
        project: str,
        definition_id: str,
        key: str,
        request: Record,
    ) -> Record:
        require(
            self.environment is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Authoring release environment is unavailable",
            503,
        )
        payload = {"definition_id": definition_id, **request}
        ControlService.validate_key(key)
        with self.store.transaction() as uow:
            _authorize(uow, actor, project, "EDITOR")
            replay = uow.delivery.idempotency(
                actor, project, "freeze_workflow_candidate", key, digest(payload)
            )
            if replay is not None:
                return replay
            revision = uow.authoring.revision(project, definition_id, request["revision"])
            validation = uow.authoring.validation(project, request["validation_id"])
            require(
                revision["source_digest"] == request["source_digest"]
                and validation["definition_id"] == definition_id
                and validation["revision"] == revision["revision"]
                and validation["source_digest"] == revision["source_digest"]
                and validation["status"] == "PASSED"
                and not environment_stale_reasons(validation["context"], self.environment),
                "REVISION_CONFLICT",
                "A current PASSED validation is required",
                409,
            )
            source = revision["source"]
        expected = PackageProcessor.freeze(source)
        require(
            bool(expected.get("invocations")),
            "INVALID_BINDING",
            "Studio candidates require at least one trusted compute node",
            422,
        )
        expected_digest = canonical_digest(expected)

        def commit(uow):
            current = uow.authoring.revision(project, definition_id, request["revision"])
            validation = uow.authoring.validation(project, request["validation_id"])
            require(
                current["source_digest"] == request["source_digest"]
                and current["source"] == source
                and validation["definition_id"] == definition_id
                and validation["revision"] == current["revision"]
                and validation["source_digest"] == current["source_digest"]
                and validation["status"] == "PASSED"
                and validation["context_digest"] == self.environment["validation_context_digest"]
                and not environment_stale_reasons(validation["context"], self.environment),
                "REVISION_CONFLICT",
                "Workflow validation changed while freezing the candidate",
                409,
            )
            candidate_id, package_id = uow.new_id(), uow.new_id()
            now = uow.now()
            package = {
                "package_id": package_id,
                "project_id": project,
                "request": source,
                "digest": digest(source),
                "status": "QUEUED",
                "compiled": None,
                "error": None,
                "owner": None,
                "generation": 0,
                "lease_until": None,
            }
            uow.workflows.add_package(package)
            uow.authoring.add_candidate(
                {
                    "candidate_id": candidate_id,
                    "project_id": project,
                    "definition_id": definition_id,
                    "revision": current["revision"],
                    "validation_id": validation["validation_id"],
                    "package_id": package_id,
                    "source_digest": current["source_digest"],
                    "expected_manifest": expected,
                    "expected_manifest_digest": expected_digest,
                    "context": validation["context"],
                    "context_digest": validation["context_digest"],
                    "created_by": actor.principal_id,
                    "created_at": now,
                }
            )
            return {
                "candidate_id": candidate_id,
                "status": "QUEUED",
                "status_url": (
                    f"/api/v1/projects/{project}/workflow-definitions/{definition_id}/"
                    f"candidates/{candidate_id}"
                ),
            }

        return self.control._write(
            actor, project, "freeze_workflow_candidate", key, payload, "EDITOR", commit
        )

    def candidate(self, actor, project, definition_id, candidate_id):
        with self.store.transaction() as uow:
            _authorize(uow, actor, project)
            record = uow.authoring.candidate(project, definition_id, candidate_id)
            package = uow.workflows.package(project, record["package_id"])
        return _candidate_view(record, package, self.environment)

    def candidates(self, actor, project, definition_id, after, limit):
        with self.store.transaction() as uow:
            _authorize(uow, actor, project)
            uow.authoring.definition(project, definition_id)
            records = uow.authoring.candidates(project, definition_id, after, limit)
            values = [
                (record, uow.workflows.package(project, record["package_id"])) for record in records
            ]
        return [_candidate_view(record, package, self.environment) for record, package in values]

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
        workflows = Workflows(
            self.store,
            self.data,
            self.execution,
            operator_catalog=self.operator_catalog,
        )
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

    def _release_view(self, uow, record):
        trials = uow.authoring.release_trials(record["release_id"])
        candidate = uow.authoring.candidate(
            record["project_id"], record["definition_id"], record["candidate_id"]
        )
        package = uow.workflows.package(record["project_id"], candidate["package_id"])
        revision = uow.authoring.revision(
            record["project_id"], record["definition_id"], record["revision"]
        )
        definition = uow.authoring.definition(record["project_id"], record["definition_id"])
        reasons = (
            environment_stale_reasons(record["context"], self.environment)
            if self.environment is not None
            else ["VALIDATION_ENVIRONMENT_UNAVAILABLE"]
        )
        if not Workflows._package_matches(package, record["package_ref"]):
            reasons.append("PACKAGE_UNAVAILABLE")
        if record["lifecycle"] != "ACTIVE":
            reasons.append("RELEASE_" + record["lifecycle"])
        for invocation in (package.get("compiled") or {}).get("invocations", {}).values():
            target = invocation["target"]
            try:
                resolve(target, self.operator_catalog)
                if target.get("execution_binding", {}).get("kind") == "REMOTE":
                    freeze_remote_binding(uow, record["project_id"], target)
            except RuleViolation:
                reasons.append("IMPLEMENTATION_UNAVAILABLE")
        return {
            "release_id": record["release_id"],
            "project_id": record["project_id"],
            "definition_id": record["definition_id"],
            "stable_key": definition["stable_key"],
            "metadata": revision["metadata"],
            "version_label": record["version_label"],
            "candidate_id": record["candidate_id"],
            "revision": record["revision"],
            "source_digest": record["source_digest"],
            "source": revision["source"],
            "package_ref": record["package_ref"],
            "trial_ids": [item["trial_id"] for item in trials],
            "context_digest": record["context_digest"],
            "lifecycle": record["lifecycle"],
            "lifecycle_revision": record["lifecycle_revision"],
            "lifecycle_reason": record["lifecycle_reason"],
            "current_executable": not reasons,
            "unavailable_reasons": sorted(set(reasons)),
            "created_by": record["created_by"],
            "created_at": record["created_at"].isoformat(),
            "qualification": "SAMPLE_VERIFIED",
        }

    def create_release(self, actor, project, definition_id, key, request):
        payload = {"definition_id": definition_id, **request}
        version_sort = semver_sort(request["version_label"])

        def commit(uow):
            candidate = uow.authoring.candidate(project, definition_id, request["candidate_id"])
            package = uow.workflows.package(project, candidate["package_id"])
            candidate_view = _candidate_view(candidate, package, self.environment)
            require(candidate_view["current_valid"], "REVISION_CONFLICT", "Candidate is stale", 409)
            for invocation in package["compiled"]["invocations"].values():
                target = invocation["target"]
                resolve(target, self.operator_catalog)
                if target.get("execution_binding", {}).get("kind") == "REMOTE":
                    freeze_remote_binding(uow, project, target)
            frozen_trials = []
            for ordinal, trial_id in enumerate(request["trial_ids"]):
                trial = uow.authoring.trial(project, definition_id, trial_id)
                action = uow.actions.get(trial["action_id"])
                require(
                    trial["candidate_id"] == candidate["candidate_id"]
                    and trial["case_verdict"] == "PASS"
                    and trial["case_result_digest"] is not None
                    and trial["case_result"].get("trial_case_digest") == trial["trial_case_digest"]
                    and trial["case_result"].get("action_id") == trial["action_id"]
                    and trial["case_result"].get("execution_id") == action.get("execution_id")
                    and trial["case_result"].get("execution_state") == "SUCCEEDED"
                    and trial["case_result"].get("publication_state") == "COMMITTED",
                    "PRECONDITION_UNRESOLVED",
                    "Release trials must be successful PASS trials for this candidate",
                    409,
                )
                frozen_trials.append(
                    {
                        "release_id": None,
                        "trial_id": trial_id,
                        "ordinal": ordinal,
                        "trial_case_digest": trial["trial_case_digest"],
                        "case_result_digest": trial["case_result_digest"],
                    }
                )
            require(
                any(
                    uow.authoring.trial(project, definition_id, item["trial_id"])[
                        "case_result"
                    ].get("started_steps", 0)
                    > 0
                    for item in frozen_trials
                ),
                "PRECONDITION_UNRESOLVED",
                "A release requires at least one PASS trial with real compute",
                409,
            )
            release_id = uow.new_id()
            for item in frozen_trials:
                item["release_id"] = release_id
            package_ref = candidate_view["package_ref"]
            require(package_ref is not None, "PRECONDITION_UNRESOLVED", "Package is not ready")
            record = {
                "release_id": release_id,
                "project_id": project,
                "definition_id": definition_id,
                "version_label": request["version_label"],
                "version_sort": version_sort,
                "candidate_id": candidate["candidate_id"],
                "revision": candidate["revision"],
                "source_digest": candidate["source_digest"],
                "package_ref": package_ref,
                "context": candidate["context"],
                "context_digest": candidate["context_digest"],
                "lifecycle": "ACTIVE",
                "lifecycle_revision": 1,
                "lifecycle_reason": "INITIAL_RELEASE",
                "created_by": actor.principal_id,
                "created_at": uow.now(),
            }
            uow.authoring.add_release(record, frozen_trials)
            return self._release_view(uow, record)

        return self.control._write(
            actor, project, "publish_workflow_release", key, payload, "EDITOR", commit
        )

    def release(self, actor, project, definition_id, version_label):
        with self.store.transaction() as uow:
            _authorize(uow, actor, project)
            return self._release_view(
                uow, uow.authoring.release(project, definition_id, version_label)
            )

    def releases(self, actor, project, definition_id, after, limit):
        with self.store.transaction() as uow:
            _authorize(uow, actor, project)
            uow.authoring.definition(project, definition_id)
            return [
                self._release_view(uow, item)
                for item in uow.authoring.releases(project, definition_id, after, limit)
            ]

    def project_releases(self, actor, project, after, limit):
        with self.store.transaction() as uow:
            _authorize(uow, actor, project)
            return [
                self._release_view(uow, item)
                for item in uow.authoring.project_releases(project, after, limit)
            ]

    @staticmethod
    def _lifecycle_event_view(record):
        return {
            "event_id": record["event_id"],
            "release_id": record["release_id"],
            "lifecycle_revision": record["lifecycle_revision"],
            "previous_lifecycle": record["previous_lifecycle"],
            "lifecycle": record["lifecycle"],
            "reason": record["reason"],
            "created_by": record["created_by"],
            "created_at": record["created_at"].isoformat(),
        }

    def release_lifecycle(self, actor, project, definition_id, version_label, after, limit):
        with self.store.transaction() as uow:
            _authorize(uow, actor, project)
            release = uow.authoring.release(project, definition_id, version_label)
            return [
                self._lifecycle_event_view(item)
                for item in uow.authoring.release_lifecycle_events(
                    release["release_id"], after, limit
                )
            ]

    def transition_release(self, actor, project, definition_id, version_label, key, request):
        require(
            request.get("lifecycle") in {"ACTIVE", "DISABLED", "REVOKED"}
            and isinstance(request.get("reason"), str)
            and bool(request["reason"].strip())
            and len(request["reason"]) <= 512,
            "MALFORMED_REQUEST",
            "A valid lifecycle state and reason are required",
            422,
        )
        payload = {
            "definition_id": definition_id,
            "version_label": version_label,
            **request,
        }

        def commit(uow):
            release = uow.authoring.release(project, definition_id, version_label, lock=True)
            require(
                release["lifecycle_revision"] == request["expected_lifecycle_revision"],
                "VERSION_CONFLICT",
                "Workflow release lifecycle changed",
                409,
            )
            current, target = release["lifecycle"], request["lifecycle"]
            allowed = {
                "ACTIVE": {"DISABLED", "REVOKED"},
                "DISABLED": {"ACTIVE", "REVOKED"},
                "REVOKED": set(),
            }
            require(
                target in allowed[current],
                "INVALID_STATE",
                "Workflow release lifecycle transition is not allowed",
                409,
            )
            event = {
                "event_id": uow.new_id(),
                "release_id": release["release_id"],
                "lifecycle_revision": release["lifecycle_revision"] + 1,
                "previous_lifecycle": current,
                "lifecycle": target,
                "reason": request["reason"],
                "created_by": actor.principal_id,
                "created_at": uow.now(),
            }
            uow.authoring.transition_release(release["release_id"], event)
            return self._lifecycle_event_view(event)

        return self.control._write(
            actor,
            project,
            "revise_workflow_release_lifecycle",
            key,
            payload,
            "OWNER",
            commit,
        )

    def execute_release(self, actor, project, definition_id, version_label, key, request):
        payload = {
            "definition_id": definition_id,
            "version_label": version_label,
            **request,
        }
        workflows = Workflows(
            self.store,
            self.data,
            self.execution,
            operator_catalog=self.operator_catalog,
        )
        replay = workflows._replay(
            actor,
            project,
            key,
            payload,
            operation="submit_workflow_release",
        )
        if replay is not None:
            return replay
        with self.store.transaction() as uow:
            _authorize(uow, actor, project, "EDITOR")
            release = uow.authoring.release(project, definition_id, version_label)
            view = self._release_view(uow, release)
            require(
                view["current_executable"],
                "PRECONDITION_UNRESOLVED",
                "Workflow release is not currently executable",
                409,
            )
            package = uow.workflows.package(project, view["package_ref"]["package_id"])
            schemas = package["compiled"]["inputs"]
            release_context = {
                "release_id": release["release_id"],
                "definition_id": definition_id,
                "stable_key": view["stable_key"],
                "version_label": version_label,
                "candidate_id": release["candidate_id"],
                "revision": release["revision"],
                "source_digest": release["source_digest"],
                "package_ref": release["package_ref"],
                "context_digest": release["context_digest"],
                "lifecycle": release["lifecycle"],
                "lifecycle_revision": release["lifecycle_revision"],
                "qualification": "SAMPLE_VERIFIED",
            }
        inputs, resources = workflows._resolve_inputs(actor, project, schemas, request["inputs"])
        workflow_request = {
            "task_id": request["task_id"],
            "expected_spec_revision": request["expected_spec_revision"],
            "target": {"kind": "WORKFLOW", "package_ref": view["package_ref"]},
            "inputs": inputs,
            "resources": resources,
            "max_node_executions": request["max_node_executions"],
            **(
                {"research_context": request["research_context"]}
                if request.get("research_context") is not None
                else {}
            ),
        }
        return workflows.admit(
            actor,
            project,
            key,
            workflow_request,
            idempotency_payload=payload,
            operation="submit_workflow_release",
            authoring_release=release_context,
        )


class TrialVerdictProcessor:
    """Observe immutable execution/publication facts without scheduling new work."""

    def __init__(self, store, data, execution, *, owner, lease_seconds=30):
        self.store = store
        self.data = data
        self.execution = execution
        self.owner = owner
        self.lease_seconds = lease_seconds

    @staticmethod
    def _at_path(value: Any, path: list[Any]) -> Any:
        for component in path:
            if isinstance(component, int):
                require(
                    isinstance(value, list) and 0 <= component < len(value),
                    "INVALID_BINDING",
                    "Trial output array path does not exist",
                )
                value = value[component]
            else:
                require(
                    isinstance(value, dict) and component in value,
                    "INVALID_BINDING",
                    "Trial output record path does not exist",
                )
                value = value[component]
        return value

    @staticmethod
    def _matches_type(value: Any, schema: Any) -> bool:
        if isinstance(schema, list):
            return (value is None and "null" in schema) or any(
                item != "null" and TrialVerdictProcessor._matches_type(value, item)
                for item in schema
            )
        if isinstance(schema, dict):
            kind = schema.get("type")
            if kind == "array":
                return isinstance(value, list) and all(
                    TrialVerdictProcessor._matches_type(item, schema["items"]) for item in value
                )
            if kind == "record":
                fields = schema.get("fields", [])
                return (
                    isinstance(value, dict)
                    and set(value) == {field["name"] for field in fields}
                    and all(
                        TrialVerdictProcessor._matches_type(value[field["name"]], field["type"])
                        for field in fields
                    )
                )
            return TrialVerdictProcessor._matches_type(value, kind)
        if schema == "File":
            return isinstance(value, dict) and set(value) == {"asset", "representation_id"}
        matches = {
            "boolean": type(value) is bool,
            "int": type(value) is int,
            "long": type(value) is int,
            "float": type(value) in {int, float},
            "double": type(value) in {int, float},
            "string": isinstance(value, str),
        }
        return matches.get(schema, False)

    @staticmethod
    def _shape(value: Any, depth: int = 0) -> Any:
        require(depth <= 16, "INVALID_BINDING", "Trial output shape is too deeply nested")
        if isinstance(value, list):
            children = [TrialVerdictProcessor._shape(item, depth + 1) for item in value]
            unique = []
            for child in children:
                if child not in unique:
                    unique.append(child)
            return {"kind": "ARRAY", "length": len(value), "item_shapes": unique}
        if isinstance(value, dict):
            if set(value) == {"asset", "representation_id"}:
                return {"kind": "FILE"}
            return {
                "kind": "RECORD",
                "fields": {
                    key: TrialVerdictProcessor._shape(value[key], depth + 1)
                    for key in sorted(value)
                },
            }
        if value is None:
            return {"kind": "NULL"}
        return {"kind": "SCALAR", "type": type(value).__name__}

    @staticmethod
    def _coverage(
        trial_case: Record, nodes: list[Record], outputs: Any
    ) -> tuple[Record, list[str]]:
        planned_values = trial_case.get("planned_steps")
        if planned_values is None:
            # A3 trial cases predate explicit coverage facts.  Their required
            # steps were the complete static plan, so project those immutable
            # facts into the A5 model instead of treating every historical
            # node as an unexpected dynamic instance.
            planned_values = [
                {**item, "expected": "EXECUTED"} for item in trial_case.get("required_steps", [])
            ]
        planned = {item["node_id"]: item for item in planned_values}
        templates = trial_case.get("deferred_templates", [])
        actual = {item["node_id"]: item for item in nodes}
        violations = []
        instances: dict[str, Record] = {}

        def bucket(path):
            key = "/".join(path)
            return instances.setdefault(
                key,
                {
                    "call_path": path,
                    "planned": 0,
                    "observed": 0,
                    "started": 0,
                    "succeeded": 0,
                    "skipped": 0,
                },
            )

        for item in planned.values():
            value = bucket(item["call_path"])
            value["planned"] += 1
            if item["expected"] == "SKIPPED":
                value["skipped"] += 1
                observed = actual.get(item["node_id"])
                if observed is not None and observed.get("executed"):
                    violations.append(item["node_id"])
        for node_id, item in actual.items():
            request = item.get("request", {})
            path = request.get("call_path", [])
            indices = request.get("scatter_indices", [])
            static = planned.get(node_id)
            dynamic = next(
                (
                    template
                    for template in templates
                    if template.get("call_path") == path
                    and template.get("index_depth") == len(indices)
                ),
                None,
            )
            if static is None and dynamic is None:
                violations.append(node_id)
            value = bucket(path)
            value["observed"] += 1
            if item.get("executed"):
                value["started"] += 1
            if (item.get("observation") or {}).get("state") == "SUCCEEDED":
                value["succeeded"] += 1
        untested = [
            {
                "call_path": item["call_path"],
                "scatter_indices": item["scatter_indices"],
                "reason": "CONDITION_FALSE",
            }
            for item in planned.values()
            if item["expected"] == "SKIPPED"
        ]
        for template in templates:
            if not any(
                item.get("request", {}).get("call_path") == template["call_path"]
                and len(item.get("request", {}).get("scatter_indices", []))
                == template["index_depth"]
                for item in nodes
            ):
                untested.append(
                    {
                        "call_path": template["call_path"],
                        "scatter_indices": None,
                        "reason": "DYNAMIC_TEMPLATE_NOT_OBSERVED",
                    }
                )
        coverage = {
            "profile": "workflow-trial-coverage-v1",
            "instances": sorted(instances.values(), key=lambda item: item["call_path"]),
            "executed_call_paths": [
                list(path)
                for path in sorted(
                    {
                        tuple(item.get("request", {}).get("call_path", []))
                        for item in nodes
                        if item.get("executed")
                    }
                )
            ],
            "untested_branches": untested,
            "final_output_shapes": {
                key: TrialVerdictProcessor._shape(value) for key, value in sorted(outputs.items())
            }
            if isinstance(outputs, dict)
            else {},
            "positive_compute": any(item.get("executed") for item in nodes),
        }
        return coverage, sorted(set(violations))

    def _finish(self, claim, verdict, result=None, category=None, retry=None):
        with self.store.transaction() as uow:
            changes = {
                "case_verdict": verdict,
                "case_result": result,
                "case_result_digest": canonical_digest(result) if result is not None else None,
                "error_category": category,
                "next_retry_at": retry,
                "finished_at": uow.now() if verdict in {"PASS", "FAIL"} or retry is None else None,
            }
            return uow.authoring.finish_trial(
                claim["trial_id"], self.owner, claim["generation"], **changes
            )

    def _retry(self, claim, category):
        with self.store.transaction() as uow:
            retry = uow.now() + timedelta(seconds=min(30, 2 ** min(claim["attempt"], 5)))
        return self._finish(claim, "ERROR", category=category, retry=retry)

    def process_one(self) -> bool:
        with self.store.transaction() as uow:
            claim = uow.authoring.claim_trial(self.owner, self.lease_seconds)
            if claim is None:
                return False
            action = uow.actions.get(claim["action_id"])
            nodes = uow.workflows.nodes(action["action_id"])
        execution_id = action.get("execution_id")
        if execution_id is None:
            with self.store.transaction() as uow:
                retry = uow.now() + timedelta(seconds=1)
            self._finish(claim, "PENDING", retry=retry)
            return True
        try:
            execution = self.execution.json(
                "GET", f"/internal/projects/{claim['project_id']}/executions/{execution_id}"
            )
            require(
                execution.get("execution_id") == execution_id
                and execution.get("action_id") == claim["action_id"],
                "INVALID_BINDING",
                "Trial execution identity changed",
            )
            if execution["state"] not in _TERMINAL:
                with self.store.transaction() as uow:
                    retry = uow.now() + timedelta(seconds=1)
                self._finish(claim, "PENDING", retry=retry)
                return True
            if execution["state"] != "SUCCEEDED":
                result = {
                    "profile": "workflow-trial-result-v1",
                    "trial_case_digest": claim["trial_case_digest"],
                    "action_id": claim["action_id"],
                    "execution_id": execution_id,
                    "execution_state": execution["state"],
                    "publication_state": "NOT_COMMITTED",
                    "started_steps": sum(bool(item["executed"]) for item in nodes),
                    "assertions": [],
                    "reasons": ["EXECUTION_" + execution["state"]],
                    "verdict": "FAIL",
                }
                self._finish(claim, "FAIL", result=result)
                return True
            publication = self.data.json(
                "GET", f"/internal/projects/{claim['project_id']}/publications/{execution_id}"
            )
            require(
                publication.get("execution_id", execution_id) == execution_id,
                "INVALID_BINDING",
                "Trial publication identity changed",
            )
            if publication.get("state") != "COMMITTED":
                if publication.get("state") == "ABORTED":
                    result = {
                        "profile": "workflow-trial-result-v1",
                        "trial_case_digest": claim["trial_case_digest"],
                        "action_id": claim["action_id"],
                        "execution_id": execution_id,
                        "execution_state": "SUCCEEDED",
                        "publication_state": "ABORTED",
                        "started_steps": sum(bool(item["executed"]) for item in nodes),
                        "assertions": [],
                        "reasons": ["PUBLICATION_ABORTED"],
                        "verdict": "FAIL",
                    }
                    self._finish(claim, "FAIL", result=result)
                    return True
                with self.store.transaction() as uow:
                    retry = uow.now() + timedelta(seconds=1)
                self._finish(claim, "PENDING", retry=retry)
                return True
            required_steps = {
                item["node_id"]: item for item in claim["trial_case"]["required_steps"]
            }
            required = set(required_steps)
            actual = {item["node_id"]: item for item in nodes}
            step_failures = [
                node_id
                for node_id in sorted(required)
                if node_id not in actual
                or not actual[node_id]["executed"]
                or (actual[node_id]["observation"] or {}).get("state") != "SUCCEEDED"
                or actual[node_id]["request"].get("call_path")
                != required_steps[node_id]["call_path"]
                or actual[node_id]["request"].get("scatter_indices")
                != required_steps[node_id]["scatter_indices"]
            ]
            outputs = publication.get("receipt", publication).get("outputs")
            # Data returns the receipt directly through its internal route after commit.
            outputs = publication.get("outputs", outputs)
            required_output_failures = [
                item["output"]
                for item in claim["trial_case"]["required_outputs"]
                if not isinstance(outputs, dict)
                or item["output"] not in outputs
                or not self._matches_type(outputs[item["output"]], item["type"])
            ]
            coverage, coverage_failures = self._coverage(claim["trial_case"], nodes, outputs)
            assertion_results = []
            assertion_failures = []
            for expectation in claim["trial_case"]["assertions"]:
                try:
                    selected = self._at_path(outputs[expectation["output"]], expectation["path"])
                    require(
                        isinstance(selected, dict)
                        and set(selected) == {"asset", "representation_id"},
                        "INVALID_BINDING",
                        "Trial assertion does not resolve to a published representation",
                    )
                    ref = selected["asset"]
                    require(
                        ref.get("project_id") == claim["project_id"],
                        "INVALID_BINDING",
                        "Trial output belongs to another project",
                    )
                    asset = self.data.json(
                        "GET",
                        f"/internal/projects/{claim['project_id']}/assets/"
                        f"{ref['asset_id']}/versions/{ref['version_id']}",
                    )
                    require(asset["ref"] == ref, "INVALID_BINDING", "Published AssetRef changed")
                    representation = next(
                        (
                            item
                            for item in asset["manifest"]["representations"]
                            if item["representation_id"] == selected["representation_id"]
                        ),
                        None,
                    )
                    passed = bool(
                        representation
                        and representation.get("schema_id") == expectation["schema_id"]
                        and (
                            expectation.get("representation_digest") is None
                            or representation.get("digest") == expectation["representation_digest"]
                        )
                    )
                except RuleViolation as problem:
                    # A committed publication can temporarily outlive the read
                    # projection that serves its assets.  Preserve the trial as
                    # retryable rather than turning transport uncertainty into
                    # an immutable scientific FAIL.
                    if problem.code in {"DEPENDENCY_UNAVAILABLE", "RESOURCE_NOT_FOUND"}:
                        raise
                    selected, passed = None, False
                except (KeyError, TypeError):
                    selected, passed = None, False
                assertion_results.append(
                    {
                        "output": expectation["output"],
                        "path": expectation["path"],
                        "schema_id": expectation["schema_id"],
                        "passed": passed,
                    }
                )
                if not passed:
                    assertion_failures.append(expectation["output"])
            verdict = (
                "PASS"
                if not step_failures
                and not required_output_failures
                and not assertion_failures
                and not coverage_failures
                else "FAIL"
            )
            reasons = (
                (["REQUIRED_STEP_FAILED"] if step_failures else [])
                + (["REQUIRED_OUTPUT_FAILED"] if required_output_failures else [])
                + (["OUTPUT_ASSERTION_FAILED"] if assertion_failures else [])
                + (["COVERAGE_IDENTITY_FAILED"] if coverage_failures else [])
            )
            result = {
                "profile": "workflow-trial-result-v1",
                "trial_case_digest": claim["trial_case_digest"],
                "action_id": claim["action_id"],
                "execution_id": execution_id,
                "execution_state": "SUCCEEDED",
                "publication_state": "COMMITTED",
                "publication_digest": canonical_digest(publication),
                "started_steps": sum(bool(item["executed"]) for item in nodes),
                "required_steps": {
                    "count": len(required),
                    "failed": step_failures,
                },
                "required_outputs": {
                    "count": len(claim["trial_case"]["required_outputs"]),
                    "failed": required_output_failures,
                },
                "assertions": assertion_results,
                "coverage": coverage,
                "coverage_failures": coverage_failures,
                "reasons": reasons,
                "verdict": verdict,
            }
            self._finish(claim, verdict, result=result)
        except RuleViolation as problem:
            if problem.code in {"DEPENDENCY_UNAVAILABLE", "RESOURCE_NOT_FOUND"}:
                self._retry(claim, "DEPENDENCY_UNAVAILABLE")
            else:
                self._finish(claim, "ERROR", category="IDENTITY_OR_PROTOCOL_CONFLICT")
        except (KeyError, TypeError, ValueError):
            self._finish(claim, "ERROR", category="IDENTITY_OR_PROTOCOL_CONFLICT")
        return True
