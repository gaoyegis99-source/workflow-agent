"""Closed P5-R2 checker capability catalogue and deterministic matching.

The catalogue is application configuration, never client input.  Image identities
are injected by deployment and frozen into each coverage plan; every other field
is versioned source code and therefore changes the capability digest.
"""

import json
from pathlib import Path

from platform_core.application.control import digest
from platform_core.domain.control import require


def _contract(name):
    return "sha256:" + digest({"contract": name, "version": 1})


_DEFINITIONS = (
    {
        "capability_id": "platform-runtime-integrity-v1",
        "version": "1.0.0",
        "implementation_id": "platform-runtime-integrity-v1",
        "image_role": "CHECKER",
        "check_types": ["RUNTIME_INTEGRITY", "METHOD_PRECONDITION"],
        "matches": {"result_kinds": ["*"]},
    },
    {
        "capability_id": "p3-timeseries-reproduction-v1",
        "version": "1.0.0",
        "implementation_id": "p3-timeseries-reproduction-v1",
        "image_role": "CHECKER",
        "check_types": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
        "matches": {
            "result_kinds": ["OPERATOR_RESULT"],
            "operators": [
                "timeseries-completeness@1.0.0",
                "statistics-linear-trend@1.0.0",
            ],
            "schemas": ["p3-result-v1"],
        },
    },
    {
        "capability_id": "spatial-vector-validity-v1",
        "version": "1.0.0",
        "implementation_id": "spatial-vector-validity-v1",
        "image_role": "GDAL_CHECKER",
        "check_types": ["DATA_VALIDITY"],
        "matches": {
            "result_kinds": ["SPATIAL_VECTOR"],
            "schemas": ["spatial-vector-v1"],
        },
    },
    {
        "capability_id": "cartographic-product-integrity-v1",
        "version": "1.0.0",
        "implementation_id": "cartographic-product-integrity-v1",
        "image_role": "CHECKER",
        "check_types": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
        "matches": {
            "result_kinds": ["CARTOGRAPHIC_PRODUCT"],
            "schemas": ["cartographic-png-v1"],
        },
    },
    {
        "capability_id": "p3-descriptive-claim-support-v1",
        "version": "1.0.0",
        "implementation_id": "p3-descriptive-claim-support-v1",
        "image_role": "CHECKER",
        "check_types": ["CLAIM_SUPPORT"],
        "matches": {
            "result_kinds": ["OPERATOR_RESULT"],
            "operators": [
                "timeseries-completeness@1.0.0",
                "statistics-linear-trend@1.0.0",
            ],
            "schemas": ["p3-result-v1"],
        },
    },
    {
        "capability_id": "tabular-profile-reproduction-v1",
        "version": "1.0.0",
        "implementation_id": "tabular-profile-reproduction-v1",
        "image_role": "CHECKER",
        "check_types": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
        "matches": {
            "result_kinds": ["OPERATOR_RESULT"],
            "operators": ["table-profile@1.0.0"],
            "schemas": ["table-profile-report-v1"],
            "output_ports": ["report"],
        },
    },
    {
        "capability_id": "tabular-validation-reproduction-v1",
        "version": "1.0.0",
        "implementation_id": "tabular-validation-reproduction-v1",
        "image_role": "CHECKER",
        "check_types": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
        "matches": {
            "result_kinds": ["OPERATOR_RESULT"],
            "operators": ["table-validate@1.0.0"],
            "schemas": ["table-validation-report-v1"],
            "output_ports": ["report"],
        },
    },
    {
        "capability_id": "tabular-project-reproduction-v1",
        "version": "1.0.0",
        "implementation_id": "tabular-project-reproduction-v1",
        "image_role": "CHECKER",
        "check_types": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
        "matches": {
            "result_kinds": ["OPERATOR_RESULT"],
            "operators": ["table-project@1.0.0"],
            "schemas": ["table-project-report-v1"],
            "output_ports": ["report"],
        },
    },
    {
        "capability_id": "tabular-filter-reproduction-v1",
        "version": "1.0.0",
        "implementation_id": "tabular-filter-reproduction-v1",
        "image_role": "CHECKER",
        "check_types": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
        "matches": {
            "result_kinds": ["OPERATOR_RESULT"],
            "operators": ["table-filter@1.0.0"],
            "schemas": ["table-filter-report-v1"],
            "output_ports": ["report"],
        },
    },
    {
        "capability_id": "tabular-cast-reproduction-v1",
        "version": "1.0.0",
        "implementation_id": "tabular-cast-reproduction-v1",
        "image_role": "CHECKER",
        "check_types": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
        "matches": {
            "result_kinds": ["OPERATOR_RESULT"],
            "operators": ["table-cast@1.0.0"],
            "schemas": ["table-cast-report-v1"],
            "output_ports": ["report"],
        },
    },
    {
        "capability_id": "tabular-aggregate-reproduction-v1",
        "version": "1.0.0",
        "implementation_id": "tabular-aggregate-reproduction-v1",
        "image_role": "CHECKER",
        "check_types": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
        "matches": {
            "result_kinds": ["OPERATOR_RESULT"],
            "operators": ["table-aggregate@1.0.0"],
            "schemas": ["table-aggregate-report-v1"],
            "output_ports": ["report"],
        },
    },
)

SOURCE_IMAGES = {
    "CHECKER": "sha256:" + "1" * 64,
    "GDAL_CHECKER": "sha256:" + "2" * 64,
}


def build_catalog(images=None):
    """Return a validated immutable catalogue using deployment image IDs."""

    images = images or SOURCE_IMAGES
    require(
        set(images) == {"CHECKER", "GDAL_CHECKER"}
        and all(
            isinstance(value, str)
            and value.startswith("sha256:")
            and len(value) == 71
            and all(ch in "0123456789abcdef" for ch in value[7:])
            for value in images.values()
        ),
        "INVALID_BINDING",
        "Checker catalogue requires immutable image identities",
    )
    capabilities = []
    for definition in _DEFINITIONS:
        value = {
            **definition,
            "image_digest": images[definition["image_role"]],
            "input_contract_digest": _contract(definition["capability_id"] + ":input"),
            "output_contract_digest": _contract(definition["capability_id"] + ":output"),
            "absolute_tolerance": 1e-10,
            "relative_tolerance": 1e-8,
        }
        value["capability_digest"] = "sha256:" + digest(value)
        capabilities.append(value)
    catalog = {"schema_version": 1, "capabilities": capabilities}
    catalog["catalog_digest"] = "sha256:" + digest(catalog)
    return catalog


def load_catalog(path, images):
    """Load deployment output and prove it is the catalogue this source defines."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Checker catalogue cannot be read") from exc
    roles = value.get("images") if isinstance(value, dict) else None
    require(isinstance(roles, dict), "INVALID_BINDING", "Checker catalogue has no image lock")
    require(
        roles == images,
        "INVALID_BINDING",
        "Checker catalogue image lock differs from Platform runtime",
    )
    expected = build_catalog(roles)
    require(
        value.get("catalog") == expected,
        "INVALID_BINDING",
        "Checker catalogue differs from source or image locks",
    )
    return expected


def capability(catalog, identity):
    found = next(
        (value for value in catalog["capabilities"] if value["capability_id"] == identity), None
    )
    require(found is not None, "INVALID_BINDING", "Unknown checker capability")
    return found


def matches(capability_value, check_type, expected):
    if check_type not in capability_value["check_types"]:
        return False
    rules = capability_value["matches"]
    if rules.get("result_kinds") != ["*"] and expected.get("result_kind") not in rules.get(
        "result_kinds", []
    ):
        return False
    operator = f"{expected.get('operator_id')}@{expected.get('operator_version')}"
    return (
        (not rules.get("operators") or operator in rules["operators"])
        and (not rules.get("schemas") or expected.get("schema_id") in rules["schemas"])
        and (not rules.get("output_ports") or expected.get("output_port") in rules["output_ports"])
    )
