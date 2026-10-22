import json
import sys

from platform_contracts.release import digest, file_digest
from platform_core.application.control import ControlService
from platform_core.application.operator_proposals import OperatorProposalService
from platform_infrastructure.admin import main

from tests.authoring.a4.helpers import catalog_and_materials


def test_maintainer_cli_uses_pinned_catalog_and_release_lock(
    database, owner, monkeypatch, tmp_path, capsys
):
    actor, _ = owner
    project = ControlService(database).create_project(actor, "a4-cli", "A4 CLI")["project_id"]
    catalog, materials, _ = catalog_and_materials()
    catalog_path = tmp_path / "operator-catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    unsigned = {
        "schema_version": 2,
        "operator_catalog": {
            "digest": catalog["catalog_digest"],
            "file_sha256": file_digest(catalog_path),
        },
        "source_tree": {"digest": materials["source"]["source_digest"]},
    }
    release = unsigned | {"release_digest": digest(unsigned)}
    release_path = tmp_path / "release-lock.json"
    release_path.write_text(json.dumps(release), encoding="utf-8")
    service = OperatorProposalService(database, operator_catalog=catalog, release_lock=release)
    proposal = service.create(
        actor,
        project,
        "create",
        {"stable_key": "cli-registration", "materials": materials},
    )
    service.submit(
        actor,
        project,
        proposal["proposal_id"],
        "submit",
        {"revision": 1, "materials_digest": proposal["head"]["materials_digest"]},
    )
    monkeypatch.setenv("V7_DATABASE_URL", database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("V7_OPERATOR_CATALOG", str(catalog_path))
    monkeypatch.setenv("V7_RELEASE_LOCK", str(release_path))
    monkeypatch.setenv("V7_RELEASE_DIGEST", release["release_digest"])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "platform-admin",
            "operator-proposal-review",
            "--project-id",
            project,
            "--proposal-id",
            proposal["proposal_id"],
            "--revision",
            "1",
            "--decision",
            "APPROVED",
            "--maintainer-id",
            "release-maintainer",
            "--evidence-digest",
            "sha256:" + "d" * 64,
            "--reason",
            "Reviewed exact controlled source and tests.",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["event_type"] == "APPROVED"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "platform-admin",
            "operator-proposal-deployment-import",
            "--project-id",
            project,
            "--proposal-id",
            proposal["proposal_id"],
            "--revision",
            "1",
            "--maintainer-id",
            "release-maintainer",
            "--evidence-digest",
            "sha256:" + "e" * 64,
        ],
    )
    main()
    assert (
        json.loads(capsys.readouterr().out)["operator_catalog_digest"] == catalog["catalog_digest"]
    )
    assert (
        service.proposal(actor, project, proposal["proposal_id"])["deployment_match"]["status"]
        == "MATCHED_ACTIVE"
    )
