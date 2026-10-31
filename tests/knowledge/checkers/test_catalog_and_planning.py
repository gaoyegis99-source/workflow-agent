import json
from copy import deepcopy

import pytest
from platform_contracts.research import ClaimRevisionCreate
from platform_core.application.checker_catalog import build_catalog, load_catalog
from platform_core.application.open_acceptance import compile_coverage
from platform_core.domain.control import RuleViolation
from platform_execution.checker_catalog import load as load_execution_catalog

from tests.knowledge.identity.test_contracts import OUTPUT, exact_spec
from tests.knowledge.identity.test_planning import candidate, registration


def r2_spec(*checks, maximum=4):
    value = exact_spec()
    value["requirements"] = [
        {
            "requirement_id": "science",
            "description": "Independent scientific verification",
            "necessary": True,
            "checks": list(checks),
        }
    ]
    value["max_check_executions"] = maximum
    return value


def test_catalog_is_deterministic_and_plan_freezes_capability_versions():
    images = {"CHECKER": "sha256:" + "a" * 64, "GDAL_CHECKER": "sha256:" + "b" * 64}
    catalog = build_catalog(images)
    assert catalog == build_catalog(images)
    capabilities = catalog["capabilities"]
    assert {
        "platform-runtime-integrity-v1",
        "p3-timeseries-reproduction-v1",
        "spatial-vector-validity-v1",
        "cartographic-product-integrity-v1",
        "p3-descriptive-claim-support-v1",
        "tabular-profile-reproduction-v1",
        "tabular-validation-reproduction-v1",
    } <= {value["capability_id"] for value in capabilities}
    assert len({value["capability_digest"] for value in capabilities}) == len(capabilities)

    plan, _ = compile_coverage(
        r2_spec("DATA_VALIDITY", "RESULT_REPRODUCTION"),
        candidate(),
        [registration()],
        [],
        [],
        [],
        [],
        "registration",
        catalog,
    )
    assert plan["schema_version"] == 3
    assert plan["checker_catalog"] == catalog["catalog_digest"]
    assert [value["capability_id"] for value in plan["checker_runs"]] == [
        "platform-runtime-integrity-v1",
        "p3-timeseries-reproduction-v1",
    ]
    reproduction = plan["checker_runs"][1]
    assert reproduction["depends_on"] == ["platform-runtime-integrity-v1"]
    assert reproduction["input_roles"] == ["trend_result"]


def test_deployment_catalog_is_verified_by_platform_and_execution(tmp_path, monkeypatch):
    # This test builds its own deployment lock and must not inherit the lock
    # exported by the unified release gate.
    monkeypatch.delenv("V7_CHECKER_CATALOG", raising=False)
    monkeypatch.delenv("V7_CHECKER_IMAGE", raising=False)
    monkeypatch.delenv("V7_GDAL_CHECKER_IMAGE", raising=False)
    images = {"CHECKER": "sha256:" + "a" * 64, "GDAL_CHECKER": "sha256:" + "b" * 64}
    catalog = build_catalog(images)
    path = tmp_path / "checker-catalog.json"
    path.write_text(json.dumps({"images": images, "catalog": catalog}))
    assert load_catalog(path, images)["catalog_digest"] == catalog["catalog_digest"]
    assert (
        load_execution_catalog(path, images["CHECKER"], images["GDAL_CHECKER"])["catalog_digest"]
        == catalog["catalog_digest"]
    )
    with pytest.raises(RuleViolation):
        load_catalog(path, images | {"CHECKER": "sha256:" + "c" * 64})
    tampered = json.loads(path.read_text())
    tampered["catalog"]["capabilities"][0]["version"] = "9.9.9"
    path.write_text(json.dumps(tampered))
    with pytest.raises(RuleViolation):
        load_catalog(path, images)
    with pytest.raises(ValueError):
        load_execution_catalog(path, images["CHECKER"], images["GDAL_CHECKER"])


def test_unknown_result_has_uncovered_indeterminate_without_generic_pass():
    value = deepcopy(candidate())
    spec = r2_spec("RESULT_REPRODUCTION")
    spec["required_outputs"][0]["expected"] = {
        "result_kind": "SPATIAL_SUMMARY",
        "operator_id": "spatial-vector-summary",
        "operator_version": "1.0.0",
        "output_port": "summary",
        "schema_id": "unknown-domain-v1",
    }
    value["payload"]["role_bindings"][0]["output_ref"] |= {
        "output_port": "summary",
        "representation_id": "summary",
        "schema_id": "unknown-domain-v1",
    }
    plan, _ = compile_coverage(
        spec,
        value,
        [registration()],
        [],
        [],
        [],
        [],
        "registration",
        build_catalog(),
    )
    uncovered = [item for item in plan["items"] if item["coverage_status"] == "UNCOVERED"]
    assert len(uncovered) == 1
    assert uncovered[0]["initial_status"] == "INDETERMINATE"
    assert all(
        value["capability_id"] != "p3-timeseries-reproduction-v1" for value in plan["checker_runs"]
    )


def test_claim_assertion_contract_requires_quantity_and_whitelisted_metric():
    base = {
        "expected_spec_revision": 1,
        "analysis_registration_id": "registration",
        "proposition_type": "DESCRIPTIVE",
        "statement": "Annual slope is one metre per year",
        "scope": {"objects": ["A"]},
        "sources": [],
        "support_status": "SUPPORTED",
        "result_assertion": {
            "kind": "P3_SCALAR",
            "role_id": "trend_result",
            "group": "A",
            "metric": "slope_per_year",
        },
    }
    value = ClaimRevisionCreate.model_validate(
        base | {"quantity": {"value": 1.0, "unit": "m/year"}}
    )
    assert value.result_assertion.metric == "slope_per_year"
    try:
        ClaimRevisionCreate.model_validate(base)
    except ValueError:
        pass
    else:
        raise AssertionError("Assertion without quantity was accepted")


def test_non_descriptive_claim_remains_uncovered():
    claim = {
        "claim_id": "claim",
        "claim_revision_id": "claim-revision",
        "revision": 1,
        "payload": {
            "analysis_registration_id": "registration",
            "proposition_type": "CAUSAL",
            "support_status": "SUPPORTED",
            "quantity": {"value": 1.0, "unit": "m/year"},
            "result_assertion": {
                "kind": "P3_SCALAR",
                "role_id": "trend_result",
                "group": "A",
                "metric": "slope_per_year",
            },
        },
    }
    plan, _ = compile_coverage(
        r2_spec("CLAIM_SUPPORT"),
        candidate(),
        [registration()],
        [claim],
        [],
        [],
        [],
        "registration",
        build_catalog(),
    )
    item = next(value for value in plan["items"] if value["check_type"] == "CLAIM_SUPPORT")
    assert item["coverage_status"] == "UNCOVERED"
    assert item["initial_status"] == "INDETERMINATE"
    assert OUTPUT["schema_id"] == "p3-result-v1"
