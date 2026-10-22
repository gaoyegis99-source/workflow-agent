"""P3 application services. All external calls are outside Platform transactions."""

from platform_core.application.catalog import configured_catalog, resolve, snapshot
from platform_core.application.control import ControlService
from platform_core.application.output_identity import (
    exact_deliverable_digest,
    output_facts,
    require_expected,
)
from platform_core.application.remote_bindings import freeze_remote_binding
from platform_core.application.research import freeze_research_context
from platform_core.domain.control import Identity, RuleViolation, require


def filter_references_match_fields(parameters, fields):
    """Check frozen filter field references without importing the HTTP contract package."""
    if not isinstance(parameters, dict) or not isinstance(fields, list):
        return False
    predicate = parameters.get("predicate")
    nodes = predicate.get("nodes") if isinstance(predicate, dict) else None
    if not isinstance(nodes, list):
        return False
    field_types = {
        field.get("field_id"): field.get("type") for field in fields if isinstance(field, dict)
    }
    field_nodes = {"COMPARE", "IN", "IS_NULL", "IS_NAN", "IS_FINITE"}
    for node in nodes:
        if not isinstance(node, dict):
            return False
        kind = node.get("kind")
        if kind not in field_nodes:
            continue
        field_id = node.get("field_id")
        if not isinstance(field_id, str) or field_id not in field_types:
            return False
        physical_type = field_types[field_id]
        if kind in {"COMPARE", "IN"} and node.get("value_type") != physical_type:
            return False
        if kind in {"IS_NAN", "IS_FINITE"} and physical_type != "double":
            return False
    return True


def cast_references_match_fields(parameters, fields):
    """Check frozen cast targets against trusted fields using only standard-library types."""
    if not isinstance(parameters, dict) or not isinstance(fields, list):
        return False
    if parameters.get("semantics") != "strict-scalar-cast-v1":
        return False
    casts = parameters.get("casts")
    if not isinstance(casts, list) or not 1 <= len(casts) <= 128:
        return False
    field_types = {
        field.get("field_id"): field.get("type") for field in fields if isinstance(field, dict)
    }
    if len(field_types) != len(fields):
        return False
    supported = {"string", "int64", "double", "bool"}
    identities = []
    for cast in casts:
        if not isinstance(cast, dict) or set(cast) != {"field_id", "target_type", "on_error"}:
            return False
        field_id = cast.get("field_id")
        source_type = field_types.get(field_id)
        target_type = cast.get("target_type")
        if (
            source_type not in supported
            or target_type not in supported
            or source_type == target_type
            or cast.get("on_error") not in {"ERROR", "SET_NULL"}
        ):
            return False
        identities.append(field_id)
    return len(set(identities)) == len(identities)


def aggregate_references_match_fields(parameters, fields):
    """Validate the bounded aggregate plan against trusted fields without contracts imports."""
    if not isinstance(parameters, dict) or not isinstance(fields, list):
        return False
    if parameters.get("semantics") != "strict-table-aggregate-v1":
        return False
    group_by = parameters.get("group_by")
    measures = parameters.get("measures")
    if (
        not isinstance(group_by, list)
        or len(group_by) > 16
        or len(set(group_by)) != len(group_by)
        or parameters.get("null_key_policy") not in {"GROUP", "DROP", "ERROR"}
        or not isinstance(measures, list)
        or not 1 <= len(measures) <= 64
    ):
        return False
    field_types = {
        field.get("field_id"): field.get("type") for field in fields if isinstance(field, dict)
    }
    if len(field_types) != len(fields) or any(
        value not in {"string", "int64", "double", "bool"} for value in field_types.values()
    ):
        return False
    if any(field_types.get(identity) not in {"string", "int64", "bool"} for identity in group_by):
        return False
    operations = {"COUNT_ROWS", "COUNT_NON_NULL", "COUNT_DISTINCT", "MIN", "MAX", "SUM", "MEAN"}
    identities = []
    for measure in measures:
        if not isinstance(measure, dict) or not {"measure_id", "operation"} <= set(measure):
            return False
        if not set(measure) <= {"measure_id", "operation", "field_id", "display_name"}:
            return False
        identity, operation, field_id = (
            measure.get("measure_id"),
            measure.get("operation"),
            measure.get("field_id"),
        )
        if not isinstance(identity, str) or operation not in operations:
            return False
        if operation == "COUNT_ROWS":
            if field_id is not None:
                return False
        else:
            source_type = field_types.get(field_id)
            if source_type is None or (
                operation in {"SUM", "MEAN"} and source_type not in {"int64", "double"}
            ):
                return False
        identities.append(identity)
    return len(set(identities)) == len(identities)


class Science:
    def __init__(self, store, data, execution, *, operator_catalog=None):
        self.store, self.data, self.execution = store, data, execution
        self.operator_catalog = operator_catalog or configured_catalog()
        self.control = ControlService(store)

    def authorize(self, actor, project, role="VIEWER"):
        with self.store.transaction() as uow:
            if actor.token_id is None:
                uow.access.authorize_principal(actor.principal_id, project, role)
            else:
                uow.access.authorize(actor, project, role)

    def data_call(self, actor, project, method, suffix, key=None, body=None, params=None):
        self.authorize(actor, project, "VIEWER" if method == "GET" else "EDITOR")
        require(
            self.data is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Data service is not configured",
            503,
        )
        headers = {"X-Actor-ID": actor.principal_id}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.data.json(
            method,
            f"/internal/projects/{project}" + suffix,
            headers=headers,
            **({"json": body} if body is not None else {}),
            **({"params": params} if params is not None else {}),
        )

    def submit(self, actor, project, key, request, *, agent_context=None):
        replay = self.control.replay_action(actor, project, key, request)
        if replay is not None:
            return replay
        self.authorize(actor, project, "EDITOR")
        with self.store.transaction() as uow:
            task = uow.tasks.get(project, request["task_id"])
            task.admissible(request["expected_spec_revision"])
            research = freeze_research_context(uow, project, task, request.get("research_context"))
        target = request["target"]
        remote = target.get("execution_binding", {}).get("kind") == "REMOTE"
        remote_binding = None
        if remote:
            with self.store.transaction() as uow:
                remote_binding = freeze_remote_binding(uow, project, target)
        else:
            resolve(target, self.operator_catalog)
        operator_definition = resolve(target, self.operator_catalog)
        input_ports = list(operator_definition["inputs"])
        require(
            len(input_ports) == 1 and set(request.get("inputs", {})) == set(input_ports),
            "INVALID_BINDING",
            "Execution inputs differ from the frozen operator contract",
            422,
        )
        input_name = input_ports[0]
        binding = self.data_call(
            actor, project, "POST", "/resolve", body=request["inputs"][input_name]
        )
        if target.get("operator_id") == "table-filter":
            require(
                filter_references_match_fields(
                    request.get("parameters"), binding.get("table", {}).get("fields")
                ),
                "INVALID_BINDING",
                "Filter predicate differs from the selected table schema",
                422,
            )
        if target.get("operator_id") == "table-cast":
            require(
                cast_references_match_fields(
                    request.get("parameters"), binding.get("table", {}).get("fields")
                ),
                "INVALID_BINDING",
                "Cast request differs from the selected table schema",
                422,
            )
        if target.get("operator_id") == "table-aggregate":
            require(
                aggregate_references_match_fields(
                    request.get("parameters"), binding.get("table", {}).get("fields")
                ),
                "INVALID_BINDING",
                "Aggregate plan differs from the selected table schema",
                422,
            )
        additions = {
            "project_id": project,
            "kind": "OPERATOR",
            "bindings": {input_name: binding},
            "resources": [binding["selection"]["asset"]],
            "_operator_snapshot": snapshot(target, self.operator_catalog),
        }
        if research is not None:
            additions["_research_policy"] = research
        if remote_binding is not None:
            additions["_remote_binding"] = remote_binding
        # Frozen legacy shape remains available to existing 0.3.x workers and checkers.
        if input_name == "observations":
            additions["binding"] = binding
        frozen = request | additions
        return self.control.submit_action(actor, project, key, frozen, agent_context=agent_context)

    def permit(self, action_id, execution_id, attempt_id):
        with self.store.transaction() as uow:
            action = uow.actions.get(action_id)
            try:
                if action["token_id"] is None:
                    uow.access.authorize_principal(
                        action["principal_id"], action["project_id"], "EDITOR"
                    )
                else:
                    uow.access.authorize(
                        Identity(action["principal_id"], action["token_id"]),
                        action["project_id"],
                        "EDITOR",
                    )
            except RuleViolation:
                return {"decision": "DENY"}
            task = uow.tasks.get(action["project_id"], action["task_id"], lock=True)
            require(
                action["execution_id"] in (None, execution_id),
                "INVALID_BINDING",
                "Execution identity changed",
            )
            request = action["request"]
            if request.get("kind") in {"CHECK", "WORKFLOW"}:
                # Acceptance owns checker admission through its separately
                # frozen Checker Catalog. Workflow parents are coordination
                # actions; each leaf is checked by permit_node. Neither is a
                # public compute operator target.
                operator_available = True
            else:
                try:
                    current_operator = resolve(
                        request.get("target", {}), self.operator_catalog, admission=False
                    )
                    operator_available = current_operator["lifecycle"] != "REVOKED"
                except RuleViolation:
                    # A pre-P7 frozen action is only recoverable while its exact
                    # compatibility definition remains present.
                    operator_available = False
            binding = request.get("target", {}).get("execution_binding", {})
            remote_enabled = True
            if binding.get("kind") == "REMOTE":
                try:
                    service = uow.service_bindings.get(
                        action["project_id"],
                        binding["service_binding_id"],
                        binding["binding_revision"],
                    )
                    remote_enabled = service["enabled"]
                except RuleViolation:
                    remote_enabled = False
            if (
                task.lifecycle != "OPEN"
                or action["cancel_requested"]
                or action["stopped"]
                or task.spec_revision != action["spec_revision"]
                or task.grant_epoch != action["grant_epoch"]
                or not remote_enabled
                or not operator_available
                or not uow.tasks.permits(
                    task.task_id, request["target"], request["resources"], uow.now()
                )
            ):
                return {"decision": "DENY"}
            if task.control == "PAUSED":
                return {"decision": "WAIT"}
            uow.science.permit(action_id, execution_id, attempt_id)
            return {
                "decision": "ALLOW",
                "spec_revision": task.spec_revision,
                "grant_epoch": task.grant_epoch,
            }

    def execution_view(self, actor, project, execution_id):
        self.authorize(actor, project)
        with self.store.transaction() as uow:
            uow.actions.by_execution(project, execution_id)
        return self.execution.json("GET", f"/internal/projects/{project}/executions/{execution_id}")

    def permit_publication(self, action_id):
        """Recheck mutable lifecycle just before immutable assets become visible."""
        with self.store.transaction() as uow:
            action = uow.actions.get(action_id)
            request = action["request"]
            if request.get("kind") == "CHECK":
                return {"decision": "ALLOW"}
            release_permit = None
            frozen_release = request.get("_workflow_release")
            if frozen_release is not None:
                release = uow.authoring.release_by_id(
                    action["project_id"], frozen_release["release_id"], lock=True
                )
                identity_matches = all(
                    release.get(key) == frozen_release.get(key)
                    for key in (
                        "release_id",
                        "definition_id",
                        "version_label",
                        "candidate_id",
                        "revision",
                        "source_digest",
                        "package_ref",
                        "context_digest",
                    )
                )
                if not identity_matches:
                    return {"decision": "DENY", "reason": "RELEASE_IDENTITY_CHANGED"}
                release_permit = uow.authoring.release_publication_permit(action_id)
                if release_permit is None:
                    decision = "DENY" if release["lifecycle"] == "REVOKED" else "ALLOW"
                    release_permit = {
                        "action_id": action_id,
                        "release_id": release["release_id"],
                        "lifecycle_revision": release["lifecycle_revision"],
                        "decision": decision,
                        "reason": ("RELEASE_REVOKED" if decision == "DENY" else "PERMIT_GRANTED"),
                        "created_at": uow.now(),
                    }
                    uow.authoring.add_release_publication_permit(release_permit)
                if release_permit["decision"] != "ALLOW":
                    return {
                        "decision": "DENY",
                        "reason": release_permit["reason"],
                        "release_lifecycle_revision": release_permit["lifecycle_revision"],
                    }
            targets = (
                list(request.get("leaf_targets", []))
                if request.get("kind") == "WORKFLOW"
                else [request.get("target", {})]
            )
            try:
                available = all(
                    resolve(target, self.operator_catalog, admission=False)["lifecycle"]
                    != "REVOKED"
                    for target in targets
                )
            except RuleViolation:
                available = False
            if not available:
                return (
                    {"decision": "DENY", "reason": "OPERATOR_REVOKED"}
                    if release_permit is not None
                    else {"decision": "DENY"}
                )
            response = {"decision": "ALLOW"}
            if release_permit is not None:
                response |= {
                    "release_lifecycle_revision": release_permit["lifecycle_revision"],
                }
            return response

    def activities(self, actor, project, task_id):
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            uow.tasks.get(project, task_id)
            rows = uow.actions.list(task_id)
            for row in rows:
                request = dict(row["request"])
                request.pop("_remote_binding", None)
                request.pop("_remote_bindings", None)
                request.pop("_operator_snapshot", None)
                row["request"] = request
            return rows

    def cancel(self, actor, project, execution_id, key, request):
        def operation(uow):
            preliminary = uow.actions.by_execution(project, execution_id)
            task = uow.tasks.get(project, preliminary["task_id"], lock=True)
            require(
                task.spec_revision == request["expected_spec_revision"],
                "REVISION_CONFLICT",
                "Task requirements changed",
            )
            action = uow.actions.get(preliminary["action_id"])
            if not action["stopped"]:
                uow.actions.update(action["action_id"], cancel_requested=True)
            return uow.actions.command(project, action["command_id"])

        return self.control._write(
            actor,
            project,
            "cancel_execution",
            key,
            request | {"execution_id": execution_id},
            "EDITOR",
            operation,
        )

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
            exact_requirements = (
                [item for item in acceptance_spec.get("required_outputs", []) if "role_id" in item]
                if acceptance_spec and acceptance_spec.get("profile") == "open-science-v1"
                else []
            )
            require(
                not canonical_bindings or bool(exact_requirements),
                "INVALID_BINDING",
                "Task specification does not define exact output roles",
            )
            require(
                not exact_requirements or bool(canonical_bindings),
                "INVALID_BINDING",
                "Exact output roles require exact deliverable bindings",
            )
            expected_by_role = {item["role_id"]: item for item in exact_requirements}
            require(
                set(binding_facts) <= set(expected_by_role),
                "INVALID_BINDING",
                "Deliverable contains an unknown output role",
            )
            for role_id, facts in binding_facts.items():
                require_expected(expected_by_role[role_id]["expected"], facts)
            from platform_core.application.control import digest

            identity_mode = "EXACT" if canonical_bindings else "LEGACY"
            fingerprint = (
                exact_deliverable_digest(canonical_bindings)
                if canonical_bindings
                else digest({"artifacts": artifacts})
            )
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
                legacy_requirements = [
                    value for value in spec["required_outputs"] if "output_id" in value
                ]
                identity_mode = candidate.get(
                    "identity_mode", candidate["payload"].get("identity_mode", "LEGACY")
                )
                if spec["delivery_class"] == "VERIFIED":
                    require(
                        identity_mode == "EXACT" and exact_requirements and not legacy_requirements,
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
                from platform_core.application.checker_catalog import build_catalog
                from platform_core.application.checker_runs import admit as admit_checker_run
                from platform_core.application.checker_runs import request_for_run
                from platform_core.application.checker_runs import reserve as reserve_checker_runs
                from platform_core.application.open_acceptance import compile_coverage

                checker_catalog = build_catalog()
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
                value
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
