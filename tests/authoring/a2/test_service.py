import os
import subprocess
import sys
from copy import deepcopy

import pytest
import sqlalchemy as sa
from platform_contracts.authoring import build_authoring_descriptor, validate_operator_parameters
from platform_core.application.authoring import (
    AuthoringService,
    AuthoringValidationProcessor,
    semantic_environment,
)
from platform_core.application.catalog import build_catalog, write_catalog
from platform_core.application.control import ControlService
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables

from scripts.lib.runtime_layout import P4_RUNTIME_ROOT
from tests.authoring.a2.helpers import profile_source


class Validator:
    def __init__(self):
        self.calls = []

    def validate(self, compiled):
        self.calls.append(compiled)


def setup(database, owner):
    actor, _ = owner
    project = ControlService(database).create_project(actor, "a2-project", "A2")
    descriptor = build_authoring_descriptor(build_catalog())
    environment = semantic_environment(descriptor, "sha256:" + "a" * 64, None)
    service = AuthoringService(
        database,
        descriptor=descriptor,
        parameter_validator=validate_operator_parameters,
        environment=environment,
    )
    definition = service.create(
        actor,
        project["project_id"],
        "create",
        {
            "expected_revision": 0,
            "stable_key": "a2-flow",
            "metadata": {"title": "A2"},
            "layout": {},
        },
    )
    revision = service.append(
        actor,
        project["project_id"],
        definition["definition_id"],
        "save",
        {
            "expected_revision": 1,
            "source_digest": definition["head"]["source_digest"],
            "source": profile_source(),
            "metadata": {"title": "A2"},
            "layout": {},
        },
    )
    return actor, project["project_id"], definition["definition_id"], revision, service, environment


def test_preview_has_no_persistence_or_budget_side_effect(database, owner):
    actor, project, _, revision, service, _ = setup(database, owner)
    result = service.preview(
        actor,
        project,
        {
            "revision_ref": {
                "definition_id": revision["definition_id"],
                "revision": revision["revision"],
                "source_digest": revision["source_digest"],
            },
            "sample_bindings": {},
            "semantic_contract_digest": service.descriptor["semantic_contract_digest"],
        },
    )
    assert result["status"] == "REQUIRES_BINDING"
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.actions)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.authoring_validations))
            == 0
        )


def test_preview_rejects_cross_project_sample_before_data_resolution(database, owner):
    actor, project, _, revision, service, _ = setup(database, owner)

    class UnexpectedData:
        def json(self, *args, **kwargs):
            raise AssertionError("cross-project input reached Data")

    service.data = UnexpectedData()
    with pytest.raises(RuleViolation) as error:
        service.preview(
            actor,
            project,
            {
                "revision_ref": {
                    "definition_id": revision["definition_id"],
                    "revision": revision["revision"],
                    "source_digest": revision["source_digest"],
                },
                "sample_bindings": {"table_input": {"asset": {"project_id": "another-project"}}},
                "semantic_contract_digest": service.descriptor["semantic_contract_digest"],
            },
        )
    assert error.value.code == "AUTHORIZATION_DENIED"


def test_validation_is_idempotent_compiles_and_fences_old_generation(database, owner):
    actor, project, definition_id, revision, service, environment = setup(database, owner)
    request = {
        "revision": revision["revision"],
        "source_digest": revision["source_digest"],
        "semantic_contract_digest": environment["semantic_contract_digest"],
    }
    receipt = service.request_validation(actor, project, definition_id, "validate", request)
    assert service.request_validation(actor, project, definition_id, "validate", request) == receipt
    validator = Validator()
    processor = AuthoringValidationProcessor(
        database,
        validator,
        service.descriptor,
        validate_operator_parameters,
        environment,
        operator_catalog=build_catalog(),
        owner="worker-new",
    )
    assert processor.process_one()
    assert validator.calls
    view = service.validation(actor, project, receipt["validation_id"])
    assert view["status"] == "PASSED"
    assert view["current_valid"]

    with database.transaction() as uow:
        assert not uow.authoring.finish_validation(
            receipt["validation_id"],
            "worker-old",
            0,
            status="REJECTED",
            issues=[],
        )


def test_frozen_environment_change_fails_without_calling_validator(database, owner):
    actor, project, definition_id, revision, service, environment = setup(database, owner)
    receipt = service.request_validation(
        actor,
        project,
        definition_id,
        "validate-stale",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "semantic_contract_digest": environment["semantic_contract_digest"],
        },
    )
    changed = deepcopy(environment)
    changed["compiler_digest"] = "sha256:" + "b" * 64
    validator = Validator()
    processor = AuthoringValidationProcessor(
        database,
        validator,
        service.descriptor,
        validate_operator_parameters,
        changed,
        operator_catalog=build_catalog(),
        owner="worker",
    )
    assert processor.process_one()
    assert validator.calls == []
    service.environment = changed
    view = service.validation(actor, project, receipt["validation_id"])
    assert view["status"] == "ERROR"
    assert not view["current_valid"]
    assert "COMPILER_DIGEST_CHANGED" in view["stale_reasons"]


def test_validator_unavailable_is_error_and_can_be_retried_with_new_request(database, owner):
    actor, project, definition_id, revision, service, environment = setup(database, owner)
    request = {
        "revision": revision["revision"],
        "source_digest": revision["source_digest"],
        "semantic_contract_digest": environment["semantic_contract_digest"],
    }
    receipt = service.request_validation(actor, project, definition_id, "first-attempt", request)

    class Unavailable:
        def validate(self, compiled):
            raise OSError("docker unavailable")

    assert AuthoringValidationProcessor(
        database,
        Unavailable(),
        service.descriptor,
        validate_operator_parameters,
        environment,
        operator_catalog=build_catalog(),
        owner="failed-worker",
    ).process_one()
    failed = service.validation(actor, project, receipt["validation_id"])
    assert failed["status"] == "ERROR"
    assert failed["issues"][-1]["code"] == "VALIDATOR_UNAVAILABLE"
    retry = service.request_validation(actor, project, definition_id, "retry-attempt", request)
    assert retry["validation_id"] != receipt["validation_id"]


def test_expired_lease_is_taken_over_and_old_generation_cannot_finish(database, owner):
    actor, project, definition_id, revision, service, environment = setup(database, owner)
    receipt = service.request_validation(
        actor,
        project,
        definition_id,
        "lease-test",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "semantic_contract_digest": environment["semantic_contract_digest"],
        },
    )
    with database.transaction() as uow:
        first = uow.authoring.claim_validation("old-worker", 30)
    with database.engine.begin() as connection:
        connection.execute(
            tables.authoring_validations.update()
            .where(tables.authoring_validations.c.validation_id == receipt["validation_id"])
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    with database.transaction() as uow:
        second = uow.authoring.claim_validation("new-worker", 30)
        assert second["generation"] == first["generation"] + 1
        assert not uow.authoring.finish_validation(
            first["validation_id"],
            "old-worker",
            first["generation"],
            status="PASSED",
            issues=[],
        )
        assert uow.authoring.finish_validation(
            second["validation_id"],
            "new-worker",
            second["generation"],
            status="PASSED",
            issues=[],
        )


def test_dedicated_worker_process_uses_fixed_cwl_container(database, owner, tmp_path):
    actor, project, definition_id, revision, service, _ = setup(database, owner)
    image = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text(encoding="utf-8").strip()
    catalog_path = tmp_path / "operator-catalog.json"
    write_catalog(catalog_path)
    environment = semantic_environment(service.descriptor, image, None)
    service.environment = environment
    receipt = service.request_validation(
        actor,
        project,
        definition_id,
        "process-validation",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "semantic_contract_digest": environment["semantic_contract_digest"],
        },
    )
    result = subprocess.run(
        [sys.executable, "-m", "platform_agent_runtime.authoring_worker", "--once"],
        env=os.environ
        | {
            "V7_DATABASE_URL": database.engine.url.render_as_string(hide_password=False),
            "V7_CWL_IMAGE": image,
            "V7_OPERATOR_CATALOG": str(catalog_path),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    validation = service.validation(actor, project, receipt["validation_id"])
    assert validation["status"] == "PASSED", validation["issues"]
