import pytest
from platform_contracts.authoring import build_authoring_descriptor, validate_operator_parameters
from platform_core.application.authoring import (
    AuthoringService,
    AuthoringValidationProcessor,
    semantic_environment,
)
from platform_core.application.authoring_release import AuthoringReleaseService
from platform_core.application.catalog import build_catalog
from platform_core.application.control import ControlService
from platform_core.application.workflow_plan import build_execution_plan
from platform_core.application.workflows import PackageProcessor
from platform_core.domain.authoring_graph import preview_graph, validate_source_envelope
from platform_core.domain.control import RuleViolation
from platform_infrastructure.cwl import CwlValidator

from scripts.lib.runtime_layout import P4_RUNTIME_ROOT
from tests.authoring.a5.helpers import advanced_source


class Validator:
    image_id = "sha256:" + "a" * 64

    def validate(self, compiled):
        assert compiled["files"]


def descriptor():
    return build_authoring_descriptor(build_catalog())


@pytest.mark.parametrize(
    ("method", "count", "indices"),
    [
        ("dotproduct", 2, [[0], [1]]),
        ("flat_crossproduct", 4, [[0, 0], [0, 1], [1, 0], [1, 1]]),
        ("nested_crossproduct", 4, [[0, 0], [0, 1], [1, 0], [1, 1]]),
    ],
)
def test_a5_source_compiles_and_plans_distinct_scatter_instances(method, count, indices):
    source = advanced_source(method)
    validate_source_envelope(source)
    frozen = PackageProcessor.freeze(source, operator_catalog=build_catalog())
    inputs = {
        "tables": [{"class": "File", "location": "one"}] * 2,
        "enabled": True,
    }
    if method != "dotproduct":
        inputs["tags"] = ["same", "same"]
    first = build_execution_plan(
        source,
        frozen,
        inputs,
        action_id="action-a5",
        max_node_executions=1000,
    )
    second = build_execution_plan(
        source,
        frozen,
        inputs,
        action_id="action-a5",
        max_node_executions=1000,
    )
    assert first == second
    assert len(first["nodes"]) == count
    assert len({item["node_id"] for item in first["nodes"]}) == count
    assert [item["scatter_indices"] for item in first["nodes"]] == indices
    assert {tuple(item["call_path"]) for item in first["nodes"]} == {("each", "profile")}


def test_condition_empty_scatter_and_node_limit_are_explicit():
    source = advanced_source()
    frozen = PackageProcessor.freeze(source, operator_catalog=build_catalog())
    skipped = build_execution_plan(
        source,
        frozen,
        {"tables": [{"class": "File", "location": "one"}], "enabled": False},
        action_id="skipped",
        max_node_executions=1,
    )
    assert [item["condition"] for item in skipped["nodes"]] == [False]

    empty = build_execution_plan(
        source,
        frozen,
        {"tables": [], "enabled": True},
        action_id="empty",
        max_node_executions=1,
    )
    assert empty["nodes"] == []

    thousand = [{"class": "File", "location": str(index)} for index in range(1000)]
    accepted = build_execution_plan(
        source,
        frozen,
        {"tables": thousand, "enabled": True},
        action_id="thousand",
        max_node_executions=1000,
    )
    assert len(accepted["nodes"]) == 1000
    with pytest.raises(RuleViolation) as raised:
        build_execution_plan(
            source,
            frozen,
            {"tables": [*thousand, {"class": "File", "location": "overflow"}], "enabled": True},
            action_id="overflow",
            max_node_executions=1000,
        )
    assert raised.value.code == "INVALID_BINDING"


def test_advanced_preview_exposes_document_path_shape_and_only_file_binding_requirement():
    source = advanced_source("nested_crossproduct")
    result = preview_graph(source, descriptor(), {}, validate_operator_parameters)
    assert result["status"] == "REQUIRES_BINDING"
    assert {
        (item["document_id"], tuple(item["call_path"]), item["node_kind"])
        for item in result["nodes"]
    } == {
        ("main", ("each",), "SUBWORKFLOW"),
        ("sub", ("each", "profile"), "OPERATOR"),
    }
    requirements = [item for item in result["issues"] if item["code"] == "REQUIRES_BINDING"]
    assert [
        (item["location"]["document_id"], item["location"]["port"]) for item in requirements
    ] == [("main", "tables")]
    subworkflow = next(item for item in result["nodes"] if item["node_kind"] == "SUBWORKFLOW")
    assert subworkflow["outputs"]["report"]["shape"] == "ARRAY"
    assert subworkflow["outputs"]["report"]["nullable"] is True


def test_formal_validation_and_candidate_use_the_same_advanced_manifest(database, owner):
    actor, _ = owner
    project = ControlService(database).create_project(actor, "a5-formal", "A5 formal")
    current_descriptor = descriptor()
    environment = semantic_environment(current_descriptor, Validator.image_id, None)
    service = AuthoringService(
        database,
        descriptor=current_descriptor,
        parameter_validator=validate_operator_parameters,
        environment=environment,
    )
    definition = service.create(
        actor,
        project["project_id"],
        "definition",
        {
            "expected_revision": 0,
            "stable_key": "a5-advanced",
            "metadata": {"title": "A5 advanced"},
            "layout": {},
        },
    )
    source = advanced_source("flat_crossproduct")
    revision = service.append(
        actor,
        project["project_id"],
        definition["definition_id"],
        "revision",
        {
            "expected_revision": 1,
            "source_digest": definition["head"]["source_digest"],
            "source": source,
            "metadata": {"title": "A5 advanced"},
            "layout": {},
        },
    )
    receipt = service.request_validation(
        actor,
        project["project_id"],
        definition["definition_id"],
        "validation",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "semantic_contract_digest": environment["semantic_contract_digest"],
        },
    )
    assert AuthoringValidationProcessor(
        database,
        Validator(),
        current_descriptor,
        validate_operator_parameters,
        environment,
        operator_catalog=build_catalog(),
        owner="a5-validation",
    ).process_one()
    validation = service.validation(actor, project["project_id"], receipt["validation_id"])
    assert validation["status"] == "PASSED"
    releases = AuthoringReleaseService(
        database,
        descriptor=current_descriptor,
        environment=environment,
        operator_catalog=build_catalog(),
    )
    candidate = releases.create_candidate(
        actor,
        project["project_id"],
        definition["definition_id"],
        "candidate",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "validation_id": validation["validation_id"],
        },
    )
    assert PackageProcessor(
        database,
        Validator(),
        operator_catalog=build_catalog(),
        owner="a5-package",
    ).process_one()
    view = releases.candidate(
        actor, project["project_id"], definition["definition_id"], candidate["candidate_id"]
    )
    assert view["status"] == "READY"
    assert (
        view["package_ref"]["manifest_digest"]
        == PackageProcessor.freeze(source, operator_catalog=build_catalog())["manifest_digest"]
    )


def test_fixed_cwltool_container_accepts_a5_generated_package():
    image = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text(encoding="utf-8").strip()
    CwlValidator(image, timeout=90).validate(
        PackageProcessor.freeze(advanced_source(), operator_catalog=build_catalog())
    )
