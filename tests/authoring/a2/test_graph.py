from platform_contracts.authoring import validate_operator_parameters
from platform_core.application.authoring import semantic_environment
from platform_core.domain.authoring_graph import (
    environment_stale_reasons,
    preview_graph,
    validate_source_envelope,
)
from platform_core.domain.control import RuleViolation

from tests.authoring.a2.helpers import clone, descriptor, profile_source, resolved_table, target


def preview(source, bindings=None):
    return preview_graph(source, descriptor(), bindings or {}, validate_operator_parameters)


def codes(result):
    return {item["code"] for item in result["issues"]}


def test_unknown_structure_is_requires_binding_and_exact_sample_resolves_it():
    source = profile_source()
    unknown = preview(source)
    assert unknown["status"] == "REQUIRES_BINDING"
    assert "REQUIRES_BINDING" in codes(unknown)

    known = preview(source, {"table_input": resolved_table(("f0", "value", "double"))})
    assert known["status"] == "COMPATIBLE"
    table = known["nodes"][0]["outputs"]["table"]
    assert table["content_role"] == "PROFILE_TABLE"
    assert table["fields"][0]["name"] == "field_id"


def test_cycles_missing_ports_unknown_implementation_and_precise_parameter_location():
    source = profile_source(parameters={"fields": ["f0", "f0"]})
    source["documents"]["main"]["steps"]["profile"]["in"]["table"] = "profile/table"
    source["documents"]["main"]["steps"]["profile"]["run"] = "missing"
    source["documents"]["main"]["outputs"] = {}
    result = preview(source)
    assert {"WORKFLOW_CYCLE", "WORKFLOW_OUTPUT_REQUIRED"} <= codes(result)

    missing = profile_source()
    missing["documents"]["main"]["steps"]["profile"]["run"] = "not_declared"
    assert "UNKNOWN_IMPLEMENTATION" in codes(preview(missing))

    disconnected = profile_source()
    del disconnected["documents"]["main"]["steps"]["profile"]["in"]["table"]
    assert "INPUT_REQUIRED" in codes(preview(disconnected))

    parameters = profile_source(parameters={"fields": ["f0", "f0"]})
    result = preview(
        parameters,
        {"table_input": resolved_table(("f0", "value", "double"))},
    )
    issue = next(item for item in result["issues"] if item["code"] == "PARAMETER_INVALID")
    assert issue["location"]["step_key"] == "profile"
    assert issue["location"]["port"] == "parameters"
    assert issue["location"]["pointer"]


def test_field_deletion_is_not_silently_accepted():
    source = profile_source(parameters={"fields": ["f1"]})
    result = preview(source, {"table_input": resolved_table(("f0", "kept", "double"))})
    issue = next(item for item in result["issues"] if item["code"] == "INPUT_FIELD_NOT_FOUND")
    assert issue["location"]["pointer"] == "/fields/0"
    assert result["status"] == "INCOMPATIBLE"

    aggregate = profile_source()
    aggregate["implementations"] = {"aggregate_impl": target("table-aggregate")}
    aggregate["documents"]["main"]["steps"] = {
        "aggregate": {
            "run": "aggregate_impl",
            "in": {
                "table": "table_input",
                "parameters": {
                    "default": {
                        "null_key_policy": "GROUP",
                        "measures": [{"measure_id": "total", "operation": "SUM", "field_id": "f0"}],
                    }
                },
            },
            "out": ["report", "table", "method"],
        }
    }
    aggregate["documents"]["main"]["outputs"]["report"]["outputSource"] = "aggregate/report"
    changed = preview(
        aggregate,
        {"table_input": resolved_table(("f0", "changed", "string"))},
    )
    assert "INPUT_FIELD_TYPE_MISMATCH" in codes(changed)


def test_png_cannot_feed_table_and_validate_output_is_not_original_table():
    source = profile_source()
    source["implementations"]["map_impl"] = target("qgis-static-map")
    source["documents"]["main"]["steps"] = {
        "map": {
            "run": "map_impl",
            "in": {
                "vector": "table_input",
                "parameters": {
                    "default": {
                        "layer_id": "l0",
                        "output_crs": "EPSG:32650",
                        "title": "Map",
                    }
                },
            },
            "out": ["image", "manifest", "method"],
        },
        "profile": {
            "run": "profile_impl",
            "in": {"table": "map/image", "parameters": {"default": {"fields": ["f0"]}}},
            "out": ["report", "table", "method"],
        },
    }
    source["documents"]["main"]["outputs"]["report"]["outputSource"] = "profile/report"
    result = preview(source, {"table_input": {"manifest": {"spatial": {"layers": []}}}})
    assert "PORT_TYPE_MISMATCH" in codes(result)

    validate = clone(profile_source())
    validate["implementations"] = {"validate_impl": target("table-validate")}
    validate["documents"]["main"]["steps"] = {
        "validate": {
            "run": "validate_impl",
            "in": {
                "table": "table_input",
                "parameters": {
                    "default": {
                        "rules": [{"rule_id": "count", "kind": "ROW_COUNT", "minimum": "1"}]
                    }
                },
            },
            "out": ["report", "table", "method"],
        }
    }
    validate["documents"]["main"]["outputs"]["report"]["outputSource"] = "validate/table"
    result = preview(validate, {"table_input": resolved_table(("f0", "value", "double"))})
    assert result["nodes"][0]["outputs"]["table"]["content_role"] == "VALIDATION_REPORT"


def test_executable_source_is_rejected_and_bounded_a5_fields_are_accepted():
    source = profile_source()
    source["documents"]["main"]["steps"]["profile"]["scatter"] = "table"
    validate_source_envelope(source)
    source["documents"]["main"]["steps"]["profile"]["baseCommand"] = "sh"
    try:
        validate_source_envelope(source)
    except RuleViolation as error:
        assert error.code == "UNSUPPORTED_TARGET"
    else:
        raise AssertionError("executable source was accepted")


def test_presentation_only_descriptor_change_does_not_stale_semantic_validation():
    frozen = semantic_environment(descriptor(), "sha256:" + "a" * 64, None)
    changed = clone(frozen)
    changed["descriptor_digest"] = "sha256:" + "b" * 64
    changed["descriptor"]["presentation_digest"] = "sha256:" + "c" * 64
    assert environment_stale_reasons(frozen, changed) == []
