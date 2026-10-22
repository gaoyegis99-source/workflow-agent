from copy import deepcopy

import pytest
from platform_core.application.control import ControlService
from platform_core.application.errors import RuleViolation
from platform_core.application.operator_proposals import (
    OperatorProposalMaintainer,
    OperatorProposalService,
)

from tests.authoring.a4.helpers import catalog_and_materials


def setup(database, owner):
    actor, _ = owner
    project = ControlService(database).create_project(actor, "a4-project", "A4")
    catalog, materials, release = catalog_and_materials()
    service = OperatorProposalService(database, operator_catalog=catalog, release_lock=release)
    return actor, project["project_id"], service, catalog, materials, release


def create(service, actor, project, materials, key="create"):
    return service.create(
        actor,
        project,
        key,
        {"stable_key": "table-profile-registration", "materials": materials},
    )


def test_create_revise_submit_and_historical_revision(database, owner):
    actor, project, service, _, materials, _ = setup(database, owner)
    proposal = create(service, actor, project, materials)
    assert proposal["status"] == "DRAFT"
    revised_materials = deepcopy(materials)
    revised_materials["limitations"] = "修订后的明确限制。"
    revision = service.append(
        actor,
        project,
        proposal["proposal_id"],
        "revise",
        {"expected_revision": 1, "materials": revised_materials},
    )
    submitted = service.submit(
        actor,
        project,
        proposal["proposal_id"],
        "submit",
        {"revision": 2, "materials_digest": revision["materials_digest"]},
    )
    assert submitted["status"] == "SUBMITTED"
    assert [item["revision"] for item in submitted["revisions"]] == [1, 2]
    assert submitted["revisions"][0]["materials"] == materials
    assert (
        service.submit(
            actor,
            project,
            proposal["proposal_id"],
            "submit",
            {"revision": 2, "materials_digest": revision["materials_digest"]},
        )
        == submitted
    )


def test_review_and_exact_deployment_are_separate(database, owner):
    actor, project, service, catalog, materials, release = setup(database, owner)
    proposal = create(service, actor, project, materials)
    submitted = service.submit(
        actor,
        project,
        proposal["proposal_id"],
        "submit",
        {"revision": 1, "materials_digest": proposal["head"]["materials_digest"]},
    )
    assert submitted["deployment_match"]["status"] == "NOT_APPROVED"
    maintainer = OperatorProposalMaintainer(
        database, operator_catalog=catalog, release_lock=release
    )
    maintainer.review(
        project,
        proposal["proposal_id"],
        1,
        "APPROVED",
        "maintainer-1",
        "sha256:" + "d" * 64,
        "Exact source and tests reviewed.",
    )
    approved = service.proposal(actor, project, proposal["proposal_id"])
    assert approved["status"] == "APPROVED"
    assert approved["deployment_match"]["status"] == "NO_RECEIPT"
    maintainer.import_deployment(
        project,
        proposal["proposal_id"],
        1,
        "maintainer-1",
        "sha256:" + "e" * 64,
    )
    deployed = service.proposal(actor, project, proposal["proposal_id"])
    assert deployed["deployment_match"] == {
        "status": "MATCHED_ACTIVE",
        "current_executable": True,
        "reasons": [],
        "current_release_digest": release["release_digest"],
        "current_catalog_digest": catalog["catalog_digest"],
    }


def test_wrong_revision_forged_release_and_undeployed_implementation_fail(database, owner):
    actor, project, service, catalog, materials, release = setup(database, owner)
    proposal = create(service, actor, project, materials)
    service.submit(
        actor,
        project,
        proposal["proposal_id"],
        "submit",
        {"revision": 1, "materials_digest": proposal["head"]["materials_digest"]},
    )
    maintainer = OperatorProposalMaintainer(
        database, operator_catalog=catalog, release_lock=release
    )
    with pytest.raises(RuleViolation) as wrong:
        maintainer.review(
            project,
            proposal["proposal_id"],
            2,
            "APPROVED",
            "maintainer",
            "sha256:" + "d" * 64,
            "wrong revision",
        )
    assert wrong.value.code == "REVISION_CONFLICT"
    maintainer.review(
        project,
        proposal["proposal_id"],
        1,
        "APPROVED",
        "maintainer",
        "sha256:" + "d" * 64,
        "approved",
    )
    forged_release = deepcopy(release)
    forged_release["operator_catalog"]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(RuleViolation):
        OperatorProposalMaintainer(
            database, operator_catalog=catalog, release_lock=forged_release
        ).import_deployment(
            project,
            proposal["proposal_id"],
            1,
            "maintainer",
            "sha256:" + "e" * 64,
        )
    forged_source = deepcopy(release)
    forged_source["source_tree"]["digest"] = "sha256:" + "9" * 64
    with pytest.raises(RuleViolation) as source_error:
        OperatorProposalMaintainer(
            database, operator_catalog=catalog, release_lock=forged_source
        ).import_deployment(
            project,
            proposal["proposal_id"],
            1,
            "maintainer",
            "sha256:" + "f" * 64,
        )
    assert source_error.value.code == "INVALID_BINDING"


def test_existing_version_semantic_overwrite_and_cross_project_read_are_rejected(database, owner):
    actor, project, service, _, materials, _ = setup(database, owner)
    proposal = create(service, actor, project, materials)
    changed = deepcopy(materials)
    changed["operator"]["outputs"] = {"other": "File"}
    # Deliberately provide a self-consistent new digest: the conflict is with
    # the already-deployed semantic identity, not malformed input.
    from platform_core.domain.authoring import canonical_digest

    semantic = {
        key: value for key, value in changed["operator"].items() if key != "operator_digest"
    }
    changed["operator"]["operator_digest"] = canonical_digest(semantic)
    revised = service.append(
        actor,
        project,
        proposal["proposal_id"],
        "revise",
        {"expected_revision": 1, "materials": changed},
    )
    with pytest.raises(RuleViolation) as conflict:
        service.submit(
            actor,
            project,
            proposal["proposal_id"],
            "submit-changed",
            {"revision": 2, "materials_digest": revised["materials_digest"]},
        )
    assert conflict.value.code == "REVISION_CONFLICT"
    other = ControlService(database).create_project(actor, "other-a4", "Other")
    with pytest.raises(RuleViolation):
        service.proposal(actor, other["project_id"], proposal["proposal_id"])


@pytest.mark.parametrize("lifecycle", ["DISABLED", "REVOKED"])
def test_lifecycle_release_is_reported_and_never_executable(database, owner, lifecycle):
    actor, project, service, catalog, materials, release = setup(database, owner)
    proposal = create(service, actor, project, materials)
    service.submit(
        actor,
        project,
        proposal["proposal_id"],
        "submit",
        {"revision": 1, "materials_digest": proposal["head"]["materials_digest"]},
    )
    OperatorProposalMaintainer(database, operator_catalog=catalog, release_lock=release).review(
        project,
        proposal["proposal_id"],
        1,
        "APPROVED",
        "maintainer",
        "sha256:" + "d" * 64,
        "approved",
    )
    changed = deepcopy(catalog)
    next(item for item in changed["operators"] if item["operator_id"] == "table-profile")[
        "lifecycle"
    ] = lifecycle
    from platform_core.domain.authoring import canonical_digest

    changed["catalog_digest"] = canonical_digest(
        {key: value for key, value in changed.items() if key != "catalog_digest"}
    )
    next_release = {
        "release_digest": "sha256:" + ("1" if lifecycle == "DISABLED" else "2") * 64,
        "operator_catalog": {"digest": changed["catalog_digest"]},
        "source_tree": {"digest": materials["source"]["source_digest"]},
    }
    maintainer = OperatorProposalMaintainer(
        database, operator_catalog=changed, release_lock=next_release
    )
    maintainer.import_deployment(
        project,
        proposal["proposal_id"],
        1,
        "maintainer",
        "sha256:" + ("3" if lifecycle == "DISABLED" else "4") * 64,
    )
    projected = OperatorProposalService(
        database, operator_catalog=changed, release_lock=next_release
    ).proposal(actor, project, proposal["proposal_id"])
    assert projected["deployment_match"]["status"] == f"MATCHED_{lifecycle}"
    assert projected["deployment_match"]["current_executable"] is False
