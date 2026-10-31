import sqlalchemy as sa
from platform_core.application.catalog import build_catalog
from platform_core.application.control import ControlService
from platform_core.application.workflows import PackageProcessor, Workflows
from platform_infrastructure import tables
from platform_infrastructure.cwl import CwlValidator

from scripts.lib.runtime_layout import P4_RUNTIME_ROOT

TARGET = {
    "kind": "OPERATOR",
    "operator_id": "timeseries-completeness",
    "version_id": "1.0.0",
    "execution_binding": {"kind": "LOCAL", "implementation_id": "p3-completeness-v1"},
}
PARAMETERS = {
    "type": "record",
    "name": "AnalysisParameters",
    "fields": [
        {"name": "groups", "type": {"type": "array", "items": "string"}},
        {"name": "start", "type": "string"},
        {"name": "end", "type": "string"},
        {"name": "frequency", "type": "string"},
        {"name": "min_samples", "type": "int"},
    ],
}
PACKAGE = {
    "name": "Completeness",
    "entrypoint": "main",
    "profile": "v7-cwl-p4-v1",
    "documents": {
        "main": {
            "cwlVersion": "v1.2",
            "class": "Workflow",
            "inputs": {"observations": "File", "parameters": PARAMETERS},
            "outputs": {
                "result": {"type": "File", "outputSource": "calculate/result"},
                "table": {"type": "File", "outputSource": "calculate/table"},
                "method": {"type": "File", "outputSource": "calculate/method"},
            },
            "steps": {
                "calculate": {
                    "run": "completeness",
                    "in": {"observations": "observations", "parameters": "parameters"},
                    "out": ["result", "table", "method"],
                }
            },
        }
    },
    "implementations": {"completeness": TARGET},
}


def create(database, actor, key="package"):
    control = ControlService(database)
    project = control.create_project(actor, key + "-project", "P4")
    value = Workflows(database, operator_catalog=build_catalog()).register(
        actor, project["project_id"], key, PACKAGE
    )
    return project["project_id"], value


def test_registration_is_idempotent_and_project_scoped(database, owner):
    actor, _ = owner
    project, first = create(database, actor)
    second = Workflows(database, operator_catalog=build_catalog()).register(
        actor, project, "package", PACKAGE
    )
    assert first == second
    assert first["status"] == "QUEUED"
    assert len(Workflows(database, operator_catalog=build_catalog()).list(actor, project)) == 1


def test_fixed_image_validation_freezes_manifest(database, owner):
    actor, _ = owner
    project, queued = create(database, actor)
    image = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip()
    processor = PackageProcessor(
        database,
        CwlValidator(image),
        operator_catalog=build_catalog(),
        owner="validator",
    )
    assert processor.process_one()
    ready = Workflows(database, operator_catalog=build_catalog()).get(
        actor, project, queued["package_id"]
    )
    assert ready["status"] == "READY"
    assert ready["package_ref"]["manifest_digest"].startswith("sha256:")
    assert ready["compiled"]["files"]
    assert not processor.process_one()


def test_old_validation_generation_cannot_publish(database, owner):
    actor, _ = owner
    project, queued = create(database, actor)
    with database.transaction() as uow:
        old = uow.workflows.claim_package("old", 0.001)
    with database.engine.begin() as connection:
        connection.execute(
            tables.workflow_packages.update()
            .where(tables.workflow_packages.c.package_id == queued["package_id"])
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    with database.transaction() as uow:
        new = uow.workflows.claim_package("new", 30)
        assert new["generation"] == old["generation"] + 1
        assert not uow.workflows.finish_package(
            old["package_id"],
            "old",
            old["generation"],
            status="READY",
            compiled={"manifest_digest": "sha256:" + "0" * 64},
            error=None,
        )
    assert (
        Workflows(database, operator_catalog=build_catalog()).get(
            actor, project, queued["package_id"]
        )["status"]
        == "VALIDATING"
    )
