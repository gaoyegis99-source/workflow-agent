"""Candidate freeze and candidate projection use cases."""

from platform_core.application.authoring import environment_stale_reasons
from platform_core.application.authoring_release_support import (
    AuthoringReleaseFlow,
)
from platform_core.application.authoring_release_support import (
    authorize as _authorize,
)
from platform_core.application.authoring_release_support import (
    candidate_view as _candidate_view,
)
from platform_core.application.control import ControlService, digest
from platform_core.application.ports import Record
from platform_core.application.workflow_processor import PackageProcessor
from platform_core.domain.authoring import canonical_digest
from platform_core.domain.control import Identity, require


class AuthoringCandidateFlow(AuthoringReleaseFlow):
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
        expected = PackageProcessor.freeze(source, operator_catalog=self.operator_catalog)
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
