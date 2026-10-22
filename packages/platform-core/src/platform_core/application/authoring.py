"""Draft workflow authoring, previews, and trusted validation use cases."""

from __future__ import annotations

from platform_core.application.control import ControlService, digest
from platform_core.application.ports import Identity, Record, Store
from platform_core.domain.authoring import (
    blank_workflow,
    canonical_digest,
    validate_definition_create,
    validate_revision_create,
)
from platform_core.domain.authoring_graph import (
    environment_stale_reasons,
    preview_graph,
    validate_source_envelope,
)
from platform_core.domain.control import RuleViolation, require


def semantic_environment(
    descriptor: Record, image_digest: str, release_digest: str | None
) -> Record:
    """Freeze the trusted inputs used by asynchronous authoring validation."""

    require(
        isinstance(image_digest, str)
        and image_digest.startswith("sha256:")
        and len(image_digest) == 71,
        "DEPENDENCY_UNAVAILABLE",
        "Trusted CWL validator image is not configured",
        503,
    )
    semantic = {
        "catalog_digest": descriptor["operator_catalog_digest"],
        "semantic_contract_digest": descriptor["semantic_contract_digest"],
        "compiler_digest": canonical_digest(
            {"compiler": "bounded-workflow-compiler", "version": "a5-v1"}
        ),
        "validator_image_digest": image_digest,
    }
    return semantic | {
        "descriptor_digest": descriptor["descriptor_digest"],
        "descriptor": descriptor,
        "release_digest": release_digest,
        "validation_context_digest": canonical_digest(semantic),
    }


def _revision_view(record: Record) -> Record:
    return record | {"created_at": record["created_at"].isoformat()}


def _definition_view(definition: Record, revision: Record) -> Record:
    return (definition | {"created_at": definition["created_at"].isoformat()}) | {
        "head": _revision_view(revision)
    }


def _validation_view(record: Record, environment: Record | None) -> Record:
    frozen = record["context"]
    stale = (
        environment_stale_reasons(frozen, environment)
        if environment
        else ["VALIDATION_ENVIRONMENT_UNAVAILABLE"]
    )
    return {
        "validation_id": record["validation_id"],
        "project_id": record["project_id"],
        "definition_id": record["definition_id"],
        "revision": record["revision"],
        "source_digest": record["source_digest"],
        "context_digest": record["context_digest"],
        "status": record["status"],
        "status_url": (
            f"/api/v1/projects/{record['project_id']}"
            f"/authoring/validations/{record['validation_id']}"
        ),
        "generation": record["generation"],
        "issues": record["issues"] or [],
        "created_by": record["created_by"],
        "created_at": record["created_at"].isoformat(),
        "started_at": record["started_at"].isoformat() if record["started_at"] else None,
        "finished_at": record["finished_at"].isoformat() if record["finished_at"] else None,
        "current_valid": record["status"] == "PASSED" and not stale,
        "stale_reasons": stale,
    }


class AuthoringService:
    def __init__(
        self,
        store: Store,
        data=None,
        *,
        descriptor: Record | None = None,
        parameter_validator=None,
        environment: Record | None = None,
    ):
        self.store = store
        self.data = data
        self.descriptor = descriptor
        self.parameter_validator = parameter_validator
        self.environment = environment

    @staticmethod
    def _authorize(uow, actor: Identity, project: str, role: str = "VIEWER") -> None:
        if actor.token_id is None:
            uow.access.authorize_principal(actor.principal_id, project, role)
        else:
            uow.access.authorize(actor, project, role)

    def authorize(self, actor: Identity, project: str) -> None:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)

    def import_source(
        self, actor: Identity, project: str, source: Record, source_format: str
    ) -> Record:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "EDITOR")
        validate_source_envelope(source)
        documents = source["documents"]
        implementations = source["implementations"]
        require(
            source["entrypoint"] in documents and not (set(documents) & set(implementations)),
            "INVALID_BINDING",
            "Imported workflow must have one unambiguous local entrypoint",
            422,
        )
        for document in documents.values():
            require(
                all(
                    step.get("run") in documents or step.get("run") in implementations
                    for step in document.get("steps", {}).values()
                ),
                "INVALID_BINDING",
                "Imported workflow contains an unresolved run reference",
                422,
            )
        return {
            "source": source,
            "source_digest": canonical_digest(source),
            "format": source_format,
            "editable": True,
        }

    def create(self, actor: Identity, project: str, key: str, request: Record) -> Record:
        ControlService.validate_key(key)
        validate_definition_create(request)
        payload_digest = digest(request)
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "EDITOR")
            replay = uow.delivery.idempotency(
                actor, project, "create_workflow_definition", key, payload_digest
            )
            if replay is not None:
                return replay
            definition_id = uow.new_id()
            now = uow.now()
            source = blank_workflow()
            revision = {
                "definition_id": definition_id,
                "project_id": project,
                "revision": 1,
                "source": source,
                "source_digest": canonical_digest(source),
                "metadata": request["metadata"],
                "layout": request.get("layout", {}),
                "layout_digest": canonical_digest(request.get("layout", {})),
                "created_by": actor.principal_id,
                "created_at": now,
            }
            definition = {
                "definition_id": definition_id,
                "project_id": project,
                "stable_key": request["stable_key"],
                "head_revision": 1,
                "created_by": actor.principal_id,
                "created_at": now,
            }
            uow.authoring.add_definition(definition, revision)
            result = _definition_view(definition, revision)
            uow.delivery.remember(actor, project, "create_workflow_definition", key, result)
            return result

    def definitions(self, actor: Identity, project: str, after: str, limit: int) -> list[Record]:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)
            return [
                _definition_view(
                    item,
                    uow.authoring.revision(project, item["definition_id"], item["head_revision"]),
                )
                for item in uow.authoring.definitions(project, after, limit)
            ]

    def definition(self, actor: Identity, project: str, definition_id: str) -> Record:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)
            value = uow.authoring.definition(project, definition_id)
            head = uow.authoring.revision(project, definition_id, value["head_revision"])
            return _definition_view(value, head)

    def append(
        self, actor: Identity, project: str, definition_id: str, key: str, request: Record
    ) -> Record:
        ControlService.validate_key(key)
        validate_revision_create(request)
        payload = {"definition_id": definition_id, **request}
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "EDITOR")
            replay = uow.delivery.idempotency(
                actor, project, "append_workflow_revision", key, digest(payload)
            )
            if replay is not None:
                return replay
            definition = uow.authoring.definition(project, definition_id, lock=True)
            require(
                definition["head_revision"] == request["expected_revision"],
                "REVISION_CONFLICT",
                "Workflow draft head revision changed",
                409,
            )
            previous = uow.authoring.revision(project, definition_id, definition["head_revision"])
            require(
                previous["source_digest"] == request["source_digest"],
                "REVISION_CONFLICT",
                "Workflow source changed",
                409,
            )
            source = request.get("source") or previous["source"]
            revision = previous | {
                "revision": definition["head_revision"] + 1,
                "source": source,
                "source_digest": canonical_digest(source),
                "metadata": request["metadata"],
                "layout": request.get("layout", {}),
                "layout_digest": canonical_digest(request.get("layout", {})),
                "created_by": actor.principal_id,
                "created_at": uow.now(),
            }
            uow.authoring.add_revision(project, definition_id, revision)
            result = _revision_view(revision)
            uow.delivery.remember(actor, project, "append_workflow_revision", key, result)
            return result

    def revisions(
        self,
        actor: Identity,
        project: str,
        definition_id: str,
        after: int,
        limit: int,
    ) -> list[Record]:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)
            uow.authoring.definition(project, definition_id)
            rows = uow.authoring.revisions(project, definition_id, after, limit)
            return [_revision_view(row) for row in rows]

    def revision(self, actor: Identity, project: str, definition_id: str, revision: int) -> Record:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)
            return _revision_view(uow.authoring.revision(project, definition_id, revision))

    def preview(self, actor: Identity, project: str, request: Record) -> Record:
        require(
            self.descriptor is not None and self.parameter_validator is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Authoring descriptor is unavailable",
            503,
        )
        require(
            request["semantic_contract_digest"] == self.descriptor["semantic_contract_digest"],
            "REVISION_CONFLICT",
            "Authoring semantic contract changed",
            409,
        )
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)
            reference = request.get("revision_ref")
            if reference:
                revision = uow.authoring.revision(
                    project, reference["definition_id"], reference["revision"]
                )
                require(
                    revision["source_digest"] == reference["source_digest"],
                    "REVISION_CONFLICT",
                    "Workflow source changed",
                    409,
                )
                source = revision["source"]
            else:
                source = request["source"]
        bindings = request.get("sample_bindings", {})
        resolved = {}
        if bindings:
            require(
                self.data is not None,
                "DEPENDENCY_UNAVAILABLE",
                "Data service is not configured",
                503,
            )
            for port, selection in bindings.items():
                asset = selection.get("asset", {}) if isinstance(selection, dict) else {}
                require(
                    asset.get("project_id") in {None, project},
                    "AUTHORIZATION_DENIED",
                    "Sample binding belongs to another project",
                    403,
                )
                resolved[port] = self.data.json(
                    "POST",
                    f"/internal/projects/{project}/resolve",
                    headers={"X-Actor-ID": actor.principal_id},
                    json=selection,
                )
        result = preview_graph(source, self.descriptor, resolved, self.parameter_validator)
        return result | {
            "source_digest": canonical_digest(source),
            "bindings_digest": canonical_digest(bindings),
            "semantic_contract_digest": self.descriptor["semantic_contract_digest"],
            "catalog_digest": self.descriptor["operator_catalog_digest"],
        }

    def request_validation(
        self,
        actor: Identity,
        project: str,
        definition_id: str,
        key: str,
        request: Record,
    ) -> Record:
        ControlService.validate_key(key)
        require(
            self.environment is not None and self.descriptor is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Trusted authoring validation is not configured",
            503,
        )
        require(
            request["semantic_contract_digest"] == self.environment["semantic_contract_digest"],
            "REVISION_CONFLICT",
            "Authoring semantic contract changed",
            409,
        )
        payload = {"definition_id": definition_id, **request}
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "EDITOR")
            replay = uow.delivery.idempotency(
                actor, project, "request_authoring_validation", key, digest(payload)
            )
            if replay is not None:
                return replay
            uow.authoring.definition(project, definition_id)
            revision = uow.authoring.revision(project, definition_id, request["revision"])
            require(
                revision["source_digest"] == request["source_digest"],
                "REVISION_CONFLICT",
                "Workflow source changed",
                409,
            )
            validation_id = uow.new_id()
            now = uow.now()
            record = {
                "validation_id": validation_id,
                "project_id": project,
                "definition_id": definition_id,
                "revision": request["revision"],
                "source_digest": revision["source_digest"],
                "source": revision["source"],
                "context": self.environment,
                "context_digest": self.environment["validation_context_digest"],
                "status": "QUEUED",
                "issues": [],
                "owner": None,
                "generation": 0,
                "lease_until": None,
                "created_by": actor.principal_id,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
            }
            uow.authoring.add_validation(record)
            receipt = {
                "validation_id": validation_id,
                "status": "QUEUED",
                "status_url": f"/api/v1/projects/{project}/authoring/validations/{validation_id}",
            }
            uow.delivery.remember(actor, project, "request_authoring_validation", key, receipt)
            return receipt

    def validation(self, actor: Identity, project: str, validation_id: str) -> Record:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)
            record = uow.authoring.validation(project, validation_id)
        return _validation_view(record, self.environment)


class AuthoringValidationProcessor:
    """Compile and validate outside a transaction, then fence the result by generation."""

    def __init__(
        self,
        store: Store,
        validator,
        descriptor: Record,
        parameter_validator,
        environment: Record,
        *,
        owner: str,
        lease_seconds: float = 120,
    ):
        self.store = store
        self.validator = validator
        self.descriptor = descriptor
        self.parameter_validator = parameter_validator
        self.environment = environment
        self.owner = owner
        self.lease_seconds = lease_seconds

    def process_one(self) -> bool:
        from platform_core.application.workflow_compiler import compile_package

        with self.store.transaction() as uow:
            claim = uow.authoring.claim_validation(self.owner, self.lease_seconds)
        if claim is None:
            return False
        stale = environment_stale_reasons(claim["context"], self.environment)
        if stale:
            status = "ERROR"
            issues = [
                {
                    "code": "VALIDATION_ENVIRONMENT_CHANGED",
                    "severity": "ERROR",
                    "scope": "SOURCE",
                    "location": {
                        "document_id": "main",
                        "step_key": None,
                        "port": None,
                        "pointer": "",
                    },
                    "message": "Trusted validation environment changed before processing",
                }
            ]
        else:
            graph = preview_graph(
                claim["source"],
                claim["context"]["descriptor"],
                {},
                self.parameter_validator,
            )
            issues = graph["issues"]
            if any(value["severity"] == "ERROR" for value in issues):
                status = "REJECTED"
            else:
                try:
                    compiled = compile_package(claim["source"])
                    self.validator.validate(compiled)
                    status = "PASSED"
                except RuleViolation as problem:
                    status = "REJECTED"
                    issues = issues + [
                        {
                            "code": problem.code,
                            "severity": "ERROR",
                            "scope": "SOURCE",
                            "location": {
                                "document_id": "main",
                                "step_key": None,
                                "port": None,
                                "pointer": "",
                            },
                            "message": problem.message,
                        }
                    ]
                except (OSError, TimeoutError, ValueError, TypeError):
                    status = "ERROR"
                    issues = issues + [
                        {
                            "code": "VALIDATOR_UNAVAILABLE",
                            "severity": "ERROR",
                            "scope": "SOURCE",
                            "location": {
                                "document_id": "main",
                                "step_key": None,
                                "port": None,
                                "pointer": "",
                            },
                            "message": "Trusted workflow validator is unavailable",
                        }
                    ]
        with self.store.transaction() as uow:
            uow.authoring.finish_validation(
                claim["validation_id"],
                self.owner,
                claim["generation"],
                status=status,
                issues=issues,
            )
        return True
