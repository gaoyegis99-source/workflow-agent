"""Capability Studio A4 operator-material and deployment-evidence use cases."""

from __future__ import annotations

from platform_core.application.catalog import validate_catalog
from platform_core.application.control import ControlService, digest
from platform_core.application.ports import Identity, Record, Store
from platform_core.domain.authoring import canonical_digest
from platform_core.domain.control import require, valid_identifier


def _material_digest(materials: Record) -> str:
    return canonical_digest(materials)


def _validate_materials(materials: Record) -> None:
    """Reuse the execution catalogue validator without registering anything."""

    require(
        isinstance(materials, dict)
        and set(materials)
        == {
            "title",
            "purpose",
            "limitations",
            "operator",
            "implementation",
            "source",
        },
        "MALFORMED_REQUEST",
        "Invalid operator proposal materials",
        400,
    )
    operator = materials["operator"]
    implementation = materials["implementation"]
    candidate = operator | {
        "lifecycle": "ACTIVE",
        "implementations": [implementation],
    }
    unsigned = {"schema_version": 1, "operators": [candidate]}
    validate_catalog(unsigned | {"catalog_digest": canonical_digest(unsigned)})


def _revision_view(record: Record) -> Record:
    return {
        "revision": record["revision"],
        "materials": record["materials"],
        "materials_digest": record["materials_digest"],
        "created_by": record["created_by"],
        "created_at": record["created_at"].isoformat(),
    }


def _event_view(record: Record) -> Record:
    return {
        key: record[key]
        for key in (
            "event_id",
            "revision",
            "materials_digest",
            "event_type",
            "reason",
            "actor_kind",
            "actor_id",
            "evidence_digest",
        )
    } | {"created_at": record["created_at"].isoformat()}


def _receipt_view(record: Record) -> Record:
    fields = (
        "receipt_id",
        "revision",
        "materials_digest",
        "release_digest",
        "operator_catalog_digest",
        "operator_id",
        "version_id",
        "operator_digest",
        "implementation_id",
        "implementation_digest",
        "image_digest",
        "source_digest",
        "tests_digest",
        "maintainer_id",
        "evidence_digest",
    )
    return {key: record[key] for key in fields} | {"created_at": record["created_at"].isoformat()}


def _status(events: list[Record], revision: int) -> str:
    current = [event for event in events if event["revision"] == revision]
    return current[-1]["event_type"] if current else "DRAFT"


def _find_operator(catalog: Record, operator_id: str, version_id: str) -> Record | None:
    return next(
        (
            item
            for item in catalog["operators"]
            if item["operator_id"] == operator_id and item["version_id"] == version_id
        ),
        None,
    )


def deployment_match(
    revision: Record,
    events: list[Record],
    receipts: list[Record],
    catalog: Record,
    release_lock: Record | None,
) -> Record:
    base = {
        "current_executable": False,
        "current_release_digest": release_lock.get("release_digest") if release_lock else None,
        "current_catalog_digest": catalog["catalog_digest"],
    }
    revision_events = [event for event in events if event["revision"] == revision["revision"]]
    if not revision_events or revision_events[-1]["event_type"] != "APPROVED":
        return base | {"status": "NOT_APPROVED", "reasons": ["REVISION_NOT_APPROVED"]}
    relevant = [item for item in receipts if item["revision"] == revision["revision"]]
    if not relevant:
        return base | {"status": "NO_RECEIPT", "reasons": ["DEPLOYMENT_RECEIPT_MISSING"]}
    receipt = relevant[-1]
    if release_lock is None or receipt["release_digest"] != release_lock.get("release_digest"):
        return base | {"status": "RELEASE_MISMATCH", "reasons": ["RELEASE_DIGEST_CHANGED"]}
    if receipt["operator_catalog_digest"] != catalog["catalog_digest"]:
        return base | {"status": "CATALOG_MISMATCH", "reasons": ["CATALOG_DIGEST_CHANGED"]}
    materials = revision["materials"]
    proposed_operator = materials["operator"]
    proposed_impl = materials["implementation"]
    source = materials["source"]
    found = _find_operator(
        catalog, proposed_operator["operator_id"], proposed_operator["version_id"]
    )
    if found is None:
        return base | {"status": "OPERATOR_NOT_FOUND", "reasons": ["OPERATOR_NOT_DEPLOYED"]}
    implementation = next(
        (
            item
            for item in found["implementations"]
            if item["implementation_id"] == proposed_impl["implementation_id"]
        ),
        None,
    )
    if implementation is None:
        return base | {
            "status": "IMPLEMENTATION_NOT_FOUND",
            "reasons": ["IMPLEMENTATION_NOT_DEPLOYED"],
        }
    expected = {
        "materials_digest": revision["materials_digest"],
        "operator_id": found["operator_id"],
        "version_id": found["version_id"],
        "operator_digest": found["operator_digest"],
        "implementation_id": implementation["implementation_id"],
        "implementation_digest": implementation["implementation_digest"],
        "image_digest": implementation["image_digest"],
        "source_digest": source["source_digest"],
        "tests_digest": source["tests_digest"],
    }
    material_matches = (
        found["operator_digest"] == proposed_operator["operator_digest"]
        and implementation == proposed_impl
        and all(receipt[key] == value for key, value in expected.items())
    )
    if not material_matches:
        return base | {"status": "MATERIAL_MISMATCH", "reasons": ["DEPLOYED_IDENTITY_DIFFERS"]}
    status = f"MATCHED_{found['lifecycle']}"
    reasons = [] if found["lifecycle"] == "ACTIVE" else [f"OPERATOR_{found['lifecycle']}"]
    return base | {
        "status": status,
        "current_executable": found["lifecycle"] == "ACTIVE",
        "reasons": reasons,
    }


class OperatorProposalService:
    def __init__(self, store: Store, *, operator_catalog: Record, release_lock: Record | None):
        self.store = store
        self.catalog = validate_catalog(operator_catalog)
        self.release_lock = release_lock

    @staticmethod
    def _authorize(uow, actor: Identity, project: str, role: str = "VIEWER") -> None:
        if actor.is_service_continuation:
            uow.access.authorize_principal(actor.principal_id, project, role)
        else:
            uow.access.authorize(actor, project, role)

    def _view(self, uow, proposal: Record) -> Record:
        revisions = uow.authoring.operator_proposal_revisions(
            proposal["project_id"], proposal["proposal_id"]
        )
        events = uow.authoring.operator_review_events(
            proposal["project_id"], proposal["proposal_id"]
        )
        receipts = uow.authoring.operator_deployment_receipts(
            proposal["project_id"], proposal["proposal_id"]
        )
        head = next(item for item in revisions if item["revision"] == proposal["head_revision"])
        return {
            "proposal_id": proposal["proposal_id"],
            "project_id": proposal["project_id"],
            "stable_key": proposal["stable_key"],
            "head_revision": proposal["head_revision"],
            "status": _status(events, proposal["head_revision"]),
            "head": _revision_view(head),
            "revisions": [_revision_view(item) for item in revisions],
            "review_events": [_event_view(item) for item in events],
            "deployment_receipts": [_receipt_view(item) for item in receipts],
            "deployment_match": deployment_match(
                head, events, receipts, self.catalog, self.release_lock
            ),
            "created_by": proposal["created_by"],
            "created_at": proposal["created_at"].isoformat(),
        }

    def create(self, actor: Identity, project: str, key: str, request: Record) -> Record:
        ControlService.validate_key(key)
        _validate_materials(request.get("materials"))
        payload_digest = digest(request)
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "EDITOR")
            replay = uow.delivery.idempotency(
                actor, project, "create_operator_proposal", key, payload_digest
            )
            if replay is not None:
                return replay
            proposal_id = uow.new_id()
            now = uow.now()
            revision = {
                "proposal_id": proposal_id,
                "revision": 1,
                "project_id": project,
                "materials": request["materials"],
                "materials_digest": _material_digest(request["materials"]),
                "created_by": actor.principal_id,
                "created_at": now,
            }
            proposal = {
                "proposal_id": proposal_id,
                "project_id": project,
                "stable_key": request["stable_key"],
                "head_revision": 1,
                "created_by": actor.principal_id,
                "created_at": now,
            }
            uow.authoring.add_operator_proposal(proposal, revision)
            result = self._view(uow, proposal)
            uow.delivery.remember(actor, project, "create_operator_proposal", key, result)
            return result

    def proposals(self, actor: Identity, project: str, after: str, limit: int) -> list[Record]:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)
            return [
                self._view(uow, item)
                for item in uow.authoring.operator_proposals(project, after, limit)
            ]

    def proposal(self, actor: Identity, project: str, proposal_id: str) -> Record:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project)
            return self._view(uow, uow.authoring.operator_proposal(project, proposal_id))

    def append(
        self, actor: Identity, project: str, proposal_id: str, key: str, request: Record
    ) -> Record:
        ControlService.validate_key(key)
        _validate_materials(request.get("materials"))
        payload_digest = digest({"proposal_id": proposal_id, **request})
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "EDITOR")
            replay = uow.delivery.idempotency(
                actor, project, f"revise_operator_proposal:{proposal_id}", key, payload_digest
            )
            if replay is not None:
                return replay
            proposal = uow.authoring.operator_proposal(project, proposal_id, lock=True)
            require(
                request["expected_revision"] == proposal["head_revision"],
                "REVISION_CONFLICT",
                "Operator proposal head changed",
                409,
            )
            events = uow.authoring.operator_review_events(project, proposal_id)
            require(
                _status(events, proposal["head_revision"]) != "SUBMITTED",
                "PRECONDITION_UNRESOLVED",
                "Submitted materials must receive a review decision before editing",
                409,
            )
            previous = uow.authoring.operator_proposal_revision(
                project, proposal_id, proposal["head_revision"]
            )
            old_identity = previous["materials"]["operator"]
            new_identity = request["materials"]["operator"]
            require(
                (old_identity["operator_id"], old_identity["version_id"])
                == (new_identity["operator_id"], new_identity["version_id"]),
                "INVALID_BINDING",
                "A proposal cannot change its operator version identity",
                409,
            )
            receipts = uow.authoring.operator_deployment_receipts(project, proposal_id)
            if receipts:
                require(
                    old_identity["operator_digest"] == new_identity["operator_digest"],
                    "REVISION_CONFLICT",
                    "A deployed operator version cannot overwrite its semantic contract",
                    409,
                )
            revision = {
                "proposal_id": proposal_id,
                "revision": proposal["head_revision"] + 1,
                "project_id": project,
                "materials": request["materials"],
                "materials_digest": _material_digest(request["materials"]),
                "created_by": actor.principal_id,
                "created_at": uow.now(),
            }
            uow.authoring.add_operator_proposal_revision(project, proposal_id, revision)
            result = _revision_view(revision)
            uow.delivery.remember(
                actor, project, f"revise_operator_proposal:{proposal_id}", key, result
            )
            return result

    def submit(
        self, actor: Identity, project: str, proposal_id: str, key: str, request: Record
    ) -> Record:
        ControlService.validate_key(key)
        payload_digest = digest({"proposal_id": proposal_id, **request})
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "EDITOR")
            replay = uow.delivery.idempotency(
                actor, project, f"submit_operator_proposal:{proposal_id}", key, payload_digest
            )
            if replay is not None:
                return replay
            proposal = uow.authoring.operator_proposal(project, proposal_id, lock=True)
            require(
                request["revision"] == proposal["head_revision"],
                "REVISION_CONFLICT",
                "Only the current proposal revision can be submitted",
                409,
            )
            revision = uow.authoring.operator_proposal_revision(
                project, proposal_id, request["revision"]
            )
            require(
                request["materials_digest"] == revision["materials_digest"],
                "INVALID_BINDING",
                "Proposal material digest differs",
                409,
            )
            events = uow.authoring.operator_review_events(project, proposal_id)
            require(
                _status(events, proposal["head_revision"])
                in {"DRAFT", "CHANGES_REQUESTED", "REJECTED"},
                "PRECONDITION_UNRESOLVED",
                "Proposal revision cannot be submitted in its current state",
                409,
            )
            proposed = revision["materials"]["operator"]
            deployed = _find_operator(self.catalog, proposed["operator_id"], proposed["version_id"])
            if deployed is not None:
                require(
                    deployed["operator_digest"] == proposed["operator_digest"],
                    "REVISION_CONFLICT",
                    "An existing operator version cannot be semantically overwritten",
                    409,
                )
            event = {
                "event_id": uow.new_id(),
                "proposal_id": proposal_id,
                "project_id": project,
                "revision": revision["revision"],
                "materials_digest": revision["materials_digest"],
                "event_type": "SUBMITTED",
                "reason": "",
                "actor_kind": "PROJECT",
                "actor_id": actor.principal_id,
                "evidence_digest": None,
                "created_at": uow.now(),
            }
            uow.authoring.add_operator_review_event(event)
            result = self._view(uow, proposal)
            uow.delivery.remember(
                actor, project, f"submit_operator_proposal:{proposal_id}", key, result
            )
            return result


class OperatorProposalMaintainer:
    """Privileged adapter used only by the local administration CLI."""

    def __init__(self, store: Store, *, operator_catalog: Record, release_lock: Record):
        self.store = store
        self.catalog = validate_catalog(operator_catalog)
        self.release_lock = release_lock

    def review(
        self,
        project: str,
        proposal_id: str,
        revision_number: int,
        decision: str,
        maintainer_id: str,
        evidence_digest: str,
        reason: str,
    ) -> Record:
        require(
            decision in {"APPROVED", "CHANGES_REQUESTED", "REJECTED"},
            "MALFORMED_REQUEST",
            "Invalid review decision",
            400,
        )
        require(
            valid_identifier(maintainer_id)
            and isinstance(evidence_digest, str)
            and len(evidence_digest) == 71
            and evidence_digest.startswith("sha256:"),
            "MALFORMED_REQUEST",
            "Maintainer and evidence identities are invalid",
            400,
        )
        require(0 < len(reason) <= 4000, "MALFORMED_REQUEST", "Review reason is required", 400)
        with self.store.transaction() as uow:
            proposal = uow.authoring.operator_proposal(project, proposal_id, lock=True)
            require(
                proposal["head_revision"] == revision_number,
                "REVISION_CONFLICT",
                "Review revision is not the current proposal revision",
                409,
            )
            revision = uow.authoring.operator_proposal_revision(
                project, proposal_id, revision_number
            )
            events = uow.authoring.operator_review_events(project, proposal_id)
            require(
                _status(events, revision_number) == "SUBMITTED",
                "PRECONDITION_UNRESOLVED",
                "Only a submitted revision can receive a decision",
                409,
            )
            event = {
                "event_id": uow.new_id(),
                "proposal_id": proposal_id,
                "project_id": project,
                "revision": revision_number,
                "materials_digest": revision["materials_digest"],
                "event_type": decision,
                "reason": reason,
                "actor_kind": "MAINTAINER",
                "actor_id": maintainer_id,
                "evidence_digest": evidence_digest,
                "created_at": uow.now(),
            }
            uow.authoring.add_operator_review_event(event)
            return _event_view(event)

    def import_deployment(
        self,
        project: str,
        proposal_id: str,
        revision_number: int,
        maintainer_id: str,
        evidence_digest: str,
    ) -> Record:
        require(
            valid_identifier(maintainer_id)
            and isinstance(evidence_digest, str)
            and len(evidence_digest) == 71
            and evidence_digest.startswith("sha256:"),
            "MALFORMED_REQUEST",
            "Maintainer and evidence identities are invalid",
            400,
        )
        with self.store.transaction() as uow:
            uow.authoring.operator_proposal(project, proposal_id, lock=True)
            revision = uow.authoring.operator_proposal_revision(
                project, proposal_id, revision_number
            )
            events = uow.authoring.operator_review_events(project, proposal_id)
            require(
                _status(events, revision_number) == "APPROVED",
                "PRECONDITION_UNRESOLVED",
                "Only an approved revision can receive deployment evidence",
                409,
            )
            materials = revision["materials"]
            proposed = materials["operator"]
            implementation = materials["implementation"]
            deployed = _find_operator(self.catalog, proposed["operator_id"], proposed["version_id"])
            require(deployed is not None, "INVALID_BINDING", "Operator is not deployed", 409)
            require(
                deployed["operator_digest"] == proposed["operator_digest"],
                "INVALID_BINDING",
                "Deployed operator semantics differ from approved materials",
                409,
            )
            deployed_impl = next(
                (
                    item
                    for item in deployed["implementations"]
                    if item["implementation_id"] == implementation["implementation_id"]
                ),
                None,
            )
            require(
                deployed_impl == implementation,
                "INVALID_BINDING",
                "Deployed implementation differs from approved materials",
                409,
            )
            require(
                self.release_lock.get("operator_catalog", {}).get("digest")
                == self.catalog["catalog_digest"],
                "INVALID_BINDING",
                "Release lock and operator catalogue differ",
                409,
            )
            source = materials["source"]
            require(
                self.release_lock.get("source_tree", {}).get("digest") == source["source_digest"],
                "INVALID_BINDING",
                "Release source identity differs from approved materials",
                409,
            )
            receipt = {
                "receipt_id": uow.new_id(),
                "proposal_id": proposal_id,
                "project_id": project,
                "revision": revision_number,
                "materials_digest": revision["materials_digest"],
                "release_digest": self.release_lock["release_digest"],
                "operator_catalog_digest": self.catalog["catalog_digest"],
                "operator_id": deployed["operator_id"],
                "version_id": deployed["version_id"],
                "operator_digest": deployed["operator_digest"],
                "implementation_id": deployed_impl["implementation_id"],
                "implementation_digest": deployed_impl["implementation_digest"],
                "image_digest": deployed_impl["image_digest"],
                "source_digest": source["source_digest"],
                "tests_digest": source["tests_digest"],
                "maintainer_id": maintainer_id,
                "evidence_digest": evidence_digest,
                "created_at": uow.now(),
            }
            uow.authoring.add_operator_deployment_receipt(receipt)
            return _receipt_view(receipt)
