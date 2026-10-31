"""Immutable P7 computation catalogue.

Service tools and acceptance checkers deliberately live in their own catalogues.
This module describes only public computation operators and their trusted
implementations. Deployments may load the generated JSON lock; the source
catalogue remains the compatibility authority for pre-P7 executions.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from platform_core.domain.control import require

SHA256 = "sha256:"
OWNERSHIPS = {"PLATFORM", "CURATED_PACKAGE", "DOMAIN_PACKAGE"}
TRUST_LEVELS = {"BUILTIN", "VERIFIED"}
LIFECYCLES = {"ACTIVE", "DISABLED", "REVOKED"}
RUNTIMES = {"NATIVE", "QGIS"}
PROTOCOLS = {
    "P3_TABLE",
    "SPATIAL_VECTOR",
    "SPATIAL_SUMMARY",
    "CARTOGRAPHIC_PNG",
    "TABULAR_V1",
}
RESOURCE_PROFILES = {
    "native-small-v1": {"cpus": "1", "memory": "1g", "pids": "64", "timeout_seconds": 120},
    "spatial-native-v1": {
        "cpus": "1",
        "memory": "1g",
        "pids": "128",
        "timeout_seconds": 120,
    },
    "qgis-medium-v1": {"cpus": "1", "memory": "2g", "pids": "128", "timeout_seconds": 180},
    "table-small-v1": {"cpus": "1", "memory": "1g", "pids": "64", "timeout_seconds": 120},
}
MAX_CATALOG_BYTES = 1024 * 1024
MAX_OPERATORS = 256
MAX_IMPLEMENTATIONS = 1024
MAX_CONTRACT_DEPTH = 16
MAX_CONTRACT_ITEMS = 4096
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SOURCE_IMAGES = {
    "OPERATOR": SHA256 + "1" * 64,
    "QGIS": SHA256 + "2" * 64,
    "SPATIAL_NATIVE": SHA256 + "3" * 64,
    "TABULAR": SHA256 + "4" * 64,
}


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return SHA256 + hashlib.sha256(encoded).hexdigest()


def _implementation(
    implementation_id: str,
    image_digest: str,
    runtime: str,
    protocol: str,
    resource_profile: str,
) -> dict:
    identity = {
        "implementation_id": implementation_id,
        "binding_kind": "LOCAL",
        "runtime": runtime,
        "image_digest": image_digest,
        "protocol": protocol,
        "resource_profile": resource_profile,
    }
    return identity | {"implementation_digest": _digest(identity)}


def _operator(
    operator_id: str,
    implementation: dict,
    inputs: dict,
    parameters: dict,
    outputs: dict,
    *,
    legacy: dict | None = None,
    ownership: str = "PLATFORM",
    trust_level: str = "BUILTIN",
) -> dict:
    semantic = {
        "operator_id": operator_id,
        "version_id": "1.0.0",
        "ownership": ownership,
        "trust_level": trust_level,
        "inputs": inputs,
        "parameters": parameters,
        "outputs": outputs,
        "effects": ["CONSUMES_EXECUTION_BUDGET", "CREATES_ASSETS"],
        "supports_workflow": True,
        "legacy": legacy,
    }
    return semantic | {
        "operator_digest": _digest(semantic),
        "lifecycle": "ACTIVE",
        "implementations": [implementation],
    }


def build_catalog(images: dict | None = None) -> dict:
    """Build the ten-operator P7.2c source catalogue using immutable image IDs."""

    images = dict(images or SOURCE_IMAGES)
    # Source definitions are deterministic.  A deployment composition root
    # supplies the image map when it builds or loads a production catalog.
    images.setdefault("TABULAR", SOURCE_IMAGES["TABULAR"])
    analysis_parameters = {
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
    table_rule = {
        "type": "record",
        "name": "TableRule",
        "fields": [
            {"name": "rule_id", "type": "string"},
            {"name": "kind", "type": "string"},
            {"name": "field_id", "type": ["null", "string"]},
            {"name": "field_ids", "type": ["null", {"type": "array", "items": "string"}]},
            {"name": "expected_type", "type": ["null", "string"]},
            {"name": "minimum", "type": ["null", "string"]},
            {"name": "maximum", "type": ["null", "string"]},
            {"name": "allowed_values", "type": ["null", {"type": "array", "items": "string"}]},
        ],
    }
    table_filter_node = {
        "type": "record",
        "name": "TableFilterNode",
        "fields": [
            {"name": "node_id", "type": "string"},
            {"name": "kind", "type": "string"},
            {"name": "field_id", "type": ["null", "string"]},
            {"name": "operator", "type": ["null", "string"]},
            {"name": "value_type", "type": ["null", "string"]},
            {"name": "value", "type": ["null", "string"]},
            {"name": "values", "type": ["null", {"type": "array", "items": "string"}]},
            {"name": "arguments", "type": {"type": "array", "items": "string"}},
        ],
    }
    table_cast_field = {
        "type": "record",
        "name": "TableCastField",
        "fields": [
            {"name": "field_id", "type": "string"},
            {"name": "target_type", "type": "string"},
            {"name": "on_error", "type": "string"},
        ],
    }
    table_aggregate_measure = {
        "type": "record",
        "name": "TableAggregateMeasure",
        "fields": [
            {"name": "measure_id", "type": "string"},
            {"name": "operation", "type": "string"},
            {"name": "field_id", "type": ["null", "string"]},
            {"name": "display_name", "type": ["null", "string"]},
        ],
    }
    operators = [
        _operator(
            "timeseries-completeness",
            _implementation(
                "p3-completeness-v1",
                images["OPERATOR"],
                "NATIVE",
                "P3_TABLE",
                "native-small-v1",
            ),
            {"observations": "File"},
            analysis_parameters,
            {"result": "File", "table": "File", "method": "File"},
            legacy={
                "operator_id": "timeseries.completeness",
                "version_id": "1.0.0",
                "source_digest": "sha256:04fc4f4a0f7a99f9dd574f3821b004eb2de2f9ec522475470bd12dc84b929e73",  # noqa: E501
            },
        ),
        _operator(
            "statistics-linear-trend",
            _implementation(
                "p3-numpy-v1",
                images["OPERATOR"],
                "NATIVE",
                "P3_TABLE",
                "native-small-v1",
            ),
            {"observations": "File"},
            analysis_parameters,
            {"result": "File", "table": "File", "method": "File"},
            legacy={
                "operator_id": "statistics.linear_trend",
                "version_id": "1.0.0",
                "source_digest": "sha256:6f33052cb398231e83e8c77e609c0337eef0b6a2c34a584549b01a8ff773e934",  # noqa: E501
            },
        ),
        _operator(
            "qgis-buffer",
            _implementation(
                "qgis-buffer-v1", images["QGIS"], "QGIS", "SPATIAL_VECTOR", "qgis-medium-v1"
            ),
            {"vector": "File"},
            {
                "type": "record",
                "name": "BufferParameters",
                "fields": [
                    {"name": "distance", "type": "double"},
                    {"name": "unit", "type": "string"},
                    {"name": "output_crs", "type": "string"},
                    {"name": "segments", "type": "int"},
                ],
            },
            {"vector": "File", "manifest": "File", "method": "File"},
        ),
        _operator(
            "spatial-vector-summary",
            _implementation(
                "spatial-vector-summary-v1",
                images["SPATIAL_NATIVE"],
                "NATIVE",
                "SPATIAL_SUMMARY",
                "spatial-native-v1",
            ),
            {"vector": "File"},
            {
                "type": "record",
                "name": "SpatialSummaryParameters",
                "fields": [{"name": "layer_id", "type": "string"}],
            },
            {"summary": "File", "method": "File"},
        ),
        _operator(
            "qgis-static-map",
            _implementation(
                "qgis-static-map-v1",
                images["QGIS"],
                "QGIS",
                "CARTOGRAPHIC_PNG",
                "qgis-medium-v1",
            ),
            {"vector": "File"},
            {
                "type": "record",
                "name": "StaticMapParameters",
                "fields": [
                    {"name": "layer_id", "type": "string"},
                    {"name": "template_id", "type": "string"},
                    {"name": "output_crs", "type": "string"},
                    {"name": "title", "type": "string"},
                    {"name": "legend", "type": "boolean"},
                    {"name": "palette", "type": "string"},
                ],
            },
            {"image": "File", "manifest": "File", "method": "File"},
        ),
        _operator(
            "table-profile",
            _implementation(
                "table-profile-native-v1",
                images["TABULAR"],
                "NATIVE",
                "TABULAR_V1",
                "table-small-v1",
            ),
            {"table": "File"},
            {
                "type": "record",
                "name": "TableProfileParameters",
                "fields": [{"name": "fields", "type": {"type": "array", "items": "string"}}],
            },
            {"report": "File", "table": "File", "method": "File"},
            ownership="CURATED_PACKAGE",
            trust_level="VERIFIED",
        ),
        _operator(
            "table-validate",
            _implementation(
                "table-validate-native-v1",
                images["TABULAR"],
                "NATIVE",
                "TABULAR_V1",
                "table-small-v1",
            ),
            {"table": "File"},
            {
                "type": "record",
                "name": "TableValidateParameters",
                "fields": [{"name": "rules", "type": {"type": "array", "items": table_rule}}],
            },
            {"report": "File", "table": "File", "method": "File"},
            ownership="CURATED_PACKAGE",
            trust_level="VERIFIED",
        ),
        _operator(
            "table-project",
            _implementation(
                "table-project-native-v1",
                images["TABULAR"],
                "NATIVE",
                "TABULAR_V1",
                "table-small-v1",
            ),
            {"table": "File"},
            {
                "type": "record",
                "name": "TableProjectParameters",
                "fields": [
                    {
                        "name": "fields",
                        "type": {
                            "type": "array",
                            "items": {
                                "type": "record",
                                "name": "TableProjectField",
                                "fields": [
                                    {"name": "source_field_id", "type": "string"},
                                    {"name": "display_name", "type": ["null", "string"]},
                                ],
                            },
                        },
                    }
                ],
            },
            {"report": "File", "table": "File", "method": "File"},
            ownership="CURATED_PACKAGE",
            trust_level="VERIFIED",
        ),
        _operator(
            "table-filter",
            _implementation(
                "table-filter-native-v1",
                images["TABULAR"],
                "NATIVE",
                "TABULAR_V1",
                "table-small-v1",
            ),
            {"table": "File"},
            {
                "type": "record",
                "name": "TableFilterParameters",
                "fields": [
                    {
                        "name": "predicate",
                        "type": {
                            "type": "record",
                            "name": "TableFilterPredicate",
                            "fields": [
                                {"name": "semantics", "type": "string"},
                                {"name": "root_id", "type": "string"},
                                {
                                    "name": "nodes",
                                    "type": {"type": "array", "items": table_filter_node},
                                },
                            ],
                        },
                    }
                ],
            },
            {"report": "File", "table": "File", "method": "File"},
            ownership="CURATED_PACKAGE",
            trust_level="VERIFIED",
        ),
        _operator(
            "table-cast",
            _implementation(
                "table-cast-native-v1",
                images["TABULAR"],
                "NATIVE",
                "TABULAR_V1",
                "table-small-v1",
            ),
            {"table": "File"},
            {
                "type": "record",
                "name": "TableCastParameters",
                "fields": [
                    {"name": "semantics", "type": "string"},
                    {"name": "casts", "type": {"type": "array", "items": table_cast_field}},
                ],
            },
            {"report": "File", "table": "File", "method": "File"},
            ownership="CURATED_PACKAGE",
            trust_level="VERIFIED",
        ),
        _operator(
            "table-aggregate",
            _implementation(
                "table-aggregate-native-v1",
                images["TABULAR"],
                "NATIVE",
                "TABULAR_V1",
                "table-small-v1",
            ),
            {"table": "File"},
            {
                "type": "record",
                "name": "TableAggregateParameters",
                "fields": [
                    {"name": "semantics", "type": "string"},
                    {"name": "group_by", "type": {"type": "array", "items": "string"}},
                    {"name": "null_key_policy", "type": "string"},
                    {
                        "name": "measures",
                        "type": {"type": "array", "items": table_aggregate_measure},
                    },
                ],
            },
            {"report": "File", "table": "File", "method": "File"},
            ownership="CURATED_PACKAGE",
            trust_level="VERIFIED",
        ),
    ]
    value = {"schema_version": 1, "operators": operators}
    value["catalog_digest"] = _digest(value)
    return validate_catalog(value)


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith(SHA256)
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _bounded_contract(value: object, *, depth: int = 0, counter: list[int] | None = None) -> bool:
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if depth > MAX_CONTRACT_DEPTH or counter[0] > MAX_CONTRACT_ITEMS:
        return False
    if value is None or type(value) in {bool, int, float}:
        return True
    if isinstance(value, str):
        return len(value) <= 4096
    if isinstance(value, list):
        return len(value) <= 1024 and all(
            _bounded_contract(item, depth=depth + 1, counter=counter) for item in value
        )
    if isinstance(value, dict):
        return len(value) <= 256 and all(
            isinstance(key, str)
            and len(key) <= 128
            and _bounded_contract(item, depth=depth + 1, counter=counter)
            for key, item in value.items()
        )
    return False


def _ports(value: object) -> bool:
    return (
        isinstance(value, dict)
        and 0 < len(value) <= 64
        and all(IDENTIFIER.fullmatch(key) and item == "File" for key, item in value.items())
    )


def validate_catalog(value: dict) -> dict:
    require(
        isinstance(value, dict)
        and set(value) == {"schema_version", "operators", "catalog_digest"}
        and value.get("schema_version") == 1
        and isinstance(value.get("operators"), list)
        and 0 < len(value["operators"]) <= MAX_OPERATORS,
        "INVALID_BINDING",
        "Invalid operator catalogue",
        422,
    )
    operator_keys, implementation_keys = set(), set()
    for operator_value in value["operators"]:
        required = {
            "operator_id",
            "version_id",
            "operator_digest",
            "ownership",
            "trust_level",
            "lifecycle",
            "inputs",
            "parameters",
            "outputs",
            "effects",
            "supports_workflow",
            "implementations",
            "legacy",
        }
        require(
            isinstance(operator_value, dict)
            and set(operator_value) == required
            and operator_value["ownership"] in OWNERSHIPS
            and operator_value["trust_level"] in TRUST_LEVELS
            and operator_value["lifecycle"] in LIFECYCLES
            and operator_value["effects"] == ["CONSUMES_EXECUTION_BUDGET", "CREATES_ASSETS"]
            and type(operator_value["supports_workflow"]) is bool
            and isinstance(operator_value["operator_id"], str)
            and IDENTIFIER.fullmatch(operator_value["operator_id"])
            and isinstance(operator_value["version_id"], str)
            and VERSION.fullmatch(operator_value["version_id"])
            and operator_value["version_id"].lower() != "latest"
            and _ports(operator_value["inputs"])
            and _ports(operator_value["outputs"])
            and isinstance(operator_value["parameters"], dict)
            and _bounded_contract(operator_value["parameters"])
            and isinstance(operator_value["implementations"], list)
            and 0 < len(operator_value["implementations"]) <= 8,
            "INVALID_BINDING",
            "Invalid operator definition",
            422,
        )
        key = (operator_value["operator_id"], operator_value["version_id"])
        require(key not in operator_keys, "INVALID_BINDING", "Duplicate operator version", 422)
        operator_keys.add(key)
        semantic = {
            name: operator_value[name]
            for name in (
                "operator_id",
                "version_id",
                "ownership",
                "trust_level",
                "inputs",
                "parameters",
                "outputs",
                "effects",
                "supports_workflow",
                "legacy",
            )
        }
        require(
            operator_value["operator_digest"] == _digest(semantic),
            "INVALID_BINDING",
            "Operator semantic digest differs",
            422,
        )
        for implementation in operator_value["implementations"]:
            identity = {
                name: implementation.get(name)
                for name in (
                    "implementation_id",
                    "binding_kind",
                    "runtime",
                    "image_digest",
                    "protocol",
                    "resource_profile",
                )
            }
            require(
                set(implementation) == set(identity) | {"implementation_digest"}
                and identity["binding_kind"] == "LOCAL"
                and identity["runtime"] in RUNTIMES
                and identity["protocol"] in PROTOCOLS
                and identity["resource_profile"] in RESOURCE_PROFILES
                and _is_digest(identity["image_digest"])
                and isinstance(identity["implementation_id"], str)
                and IDENTIFIER.fullmatch(identity["implementation_id"])
                and implementation["implementation_digest"] == _digest(identity)
                and identity["implementation_id"] not in implementation_keys,
                "INVALID_BINDING",
                "Invalid or duplicate operator implementation",
                422,
            )
            implementation_keys.add(identity["implementation_id"])
            require(
                len(implementation_keys) <= MAX_IMPLEMENTATIONS,
                "INVALID_BINDING",
                "Operator catalogue has too many implementations",
                422,
            )
    require(
        value["catalog_digest"]
        == _digest({key: item for key, item in value.items() if key != "catalog_digest"}),
        "INVALID_BINDING",
        "Operator catalogue digest differs",
        422,
    )
    return value


def load_catalog(path: str | Path) -> dict:
    try:
        source = Path(path)
        if source.stat().st_size > MAX_CATALOG_BYTES:
            raise ValueError("Operator catalogue is too large")
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Operator catalogue cannot be read") from error
    return validate_catalog(value)


def write_catalog(path: str | Path, images: dict | None = None) -> dict:
    value = build_catalog(images)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    temporary.replace(destination)
    return value


def _find(catalog: dict, operator_id: str, version_id: str) -> dict | None:
    return next(
        (
            item
            for item in catalog["operators"]
            if item["operator_id"] == operator_id and item["version_id"] == version_id
        ),
        None,
    )


def public_operator(value: dict, *, implementations: list | None = None) -> dict:
    item = deepcopy(value)
    if implementations is not None:
        item["implementations"] = implementations
    item["implementation_id"] = item["implementations"][0]["implementation_id"]
    item["runtime"] = item["implementations"][0]["runtime"]
    return item


def operators(catalog: dict, *, include_inactive: bool = False) -> list[dict]:
    catalog = validate_catalog(catalog)
    return [
        public_operator(item)
        for item in catalog["operators"]
        if include_inactive or item["lifecycle"] == "ACTIVE"
    ]


def target(operator_value: dict) -> dict:
    implementation = operator_value.get("implementations", [{}])[0]
    identity = implementation.get("implementation_id", operator_value.get("implementation_id"))
    return {
        "kind": "OPERATOR",
        "operator_id": operator_value["operator_id"],
        "version_id": operator_value["version_id"],
        "execution_binding": {"kind": "LOCAL", "implementation_id": identity},
    }


def resolve(value: dict, catalog: dict, *, admission: bool = True) -> dict:
    catalog = validate_catalog(catalog)
    found = _find(catalog, value.get("operator_id"), value.get("version_id"))
    require(found is not None, "UNSUPPORTED_TARGET", "Operator version is not registered", 422)
    if admission:
        require(
            found["lifecycle"] == "ACTIVE", "UNSUPPORTED_TARGET", "Operator is unavailable", 422
        )
    binding = value.get("execution_binding", {})
    if binding.get("kind") == "LOCAL":
        implementation = next(
            (
                item
                for item in found["implementations"]
                if item["implementation_id"] == binding.get("implementation_id")
            ),
            None,
        )
        require(
            implementation is not None,
            "UNSUPPORTED_TARGET",
            "Implementation is not registered",
            422,
        )
    else:
        require(
            found["operator_id"] == "spatial-vector-summary"
            and binding.get("kind") == "REMOTE"
            and isinstance(binding.get("service_binding_id"), str)
            and type(binding.get("binding_revision")) is int
            and binding["binding_revision"] >= 1,
            "UNSUPPORTED_TARGET",
            "Remote implementation is not registered for this operator",
            422,
        )
    return found


def registered(value: dict, catalog: dict) -> None:
    resolve(value, catalog)


def operator(value: dict, catalog: dict) -> dict:
    return public_operator(resolve(value, catalog))


def snapshot(value: dict, catalog: dict, *, admission: bool = True) -> dict:
    catalog = validate_catalog(catalog)
    found = resolve(value, catalog, admission=admission)
    binding = value["execution_binding"]
    implementation = (
        next(
            item
            for item in found["implementations"]
            if item["implementation_id"] == binding.get("implementation_id")
        )
        if binding["kind"] == "LOCAL"
        else None
    )
    return {
        "catalog_digest": catalog["catalog_digest"],
        "operator_id": found["operator_id"],
        "version_id": found["version_id"],
        "operator_digest": found["operator_digest"],
        "ownership": found["ownership"],
        "trust_level": found["trust_level"],
        "lifecycle": found["lifecycle"],
        "effects": deepcopy(found["effects"]),
        "supports_workflow": found["supports_workflow"],
        "implementation": deepcopy(implementation),
    }


def ports(value: dict, catalog: dict):
    found = resolve(value, catalog)
    return found["inputs"] | {"parameters": found["parameters"]}, found["outputs"]


OPERATORS = operators(build_catalog())
CHECK_TARGET = {
    "kind": "OPERATOR",
    "operator_id": "p3-timeseries-check",
    "version_id": "1.0.0",
    "execution_binding": {"kind": "LOCAL", "implementation_id": "p3-decimal-v1"},
}
OPEN_CHECK_TARGET = {
    "kind": "OPERATOR",
    "operator_id": "open-science-check",
    "version_id": "1.0.0",
    "execution_binding": {"kind": "LOCAL", "implementation_id": "open-science-v1"},
}
