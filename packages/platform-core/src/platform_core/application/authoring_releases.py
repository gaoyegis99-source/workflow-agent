"""Immutable workflow release and release-lifecycle use cases."""

from platform_core.application.authoring import environment_stale_reasons
from platform_core.application.authoring_release_support import (
    AuthoringReleaseFlow as AuthoringReleaseBase,
)
from platform_core.application.authoring_release_support import (
    authorize as _authorize,
)
from platform_core.application.authoring_release_support import (
    candidate_view as _candidate_view,
)
from platform_core.application.authoring_release_support import (
    semver_sort,
)
from platform_core.application.catalog import resolve
from platform_core.application.remote_bindings import freeze_remote_binding
from platform_core.application.workflow_support import WorkflowFlow
from platform_core.domain.control import RuleViolation, require


class AuthoringReleaseLifecycleFlow(AuthoringReleaseBase):
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
        if not WorkflowFlow._package_matches(package, record["package_ref"]):
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
        workflows = self.workflows
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
