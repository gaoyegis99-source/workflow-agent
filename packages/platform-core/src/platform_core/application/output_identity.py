"""Closed compatibility catalogue for exact published output identities."""

from platform_core.application.control import digest
from platform_core.domain.control import require

_SCHEMAS = {
    ("timeseries-completeness", "1.0.0", "result"): "p3-result-v1",
    ("statistics-linear-trend", "1.0.0", "result"): "p3-result-v1",
    ("timeseries-completeness", "1.0.0", "table"): "p3-table-v1",
    ("statistics-linear-trend", "1.0.0", "table"): "p3-table-v1",
    ("timeseries-completeness", "1.0.0", "method"): "p3-method-v1",
    ("statistics-linear-trend", "1.0.0", "method"): "p3-method-v1",
    ("qgis-buffer", "1.0.0", "vector"): "spatial-vector-v1",
    ("qgis-buffer", "1.0.0", "manifest"): "spatial-manifest-v1",
    ("qgis-buffer", "1.0.0", "method"): "spatial-method-v1",
    ("spatial-vector-summary", "1.0.0", "summary"): "spatial-summary-v1",
    ("spatial-vector-summary", "1.0.0", "method"): "spatial-method-v1",
    ("qgis-static-map", "1.0.0", "image"): "cartographic-png-v1",
    ("qgis-static-map", "1.0.0", "manifest"): "cartographic-manifest-v1",
    ("qgis-static-map", "1.0.0", "method"): "cartographic-method-v1",
}


def output_facts(manifest, representation_id):
    source = manifest.get("source", {})
    target = source.get("target", {})
    representation = next(
        (
            value
            for value in manifest.get("representations", [])
            if value.get("representation_id") == representation_id
        ),
        None,
    )
    require(representation is not None, "INVALID_BINDING", "Output representation is absent")
    key = (target.get("operator_id"), target.get("version_id"), representation_id)
    schema_id = representation.get("schema_id")
    schema_source = "MANIFEST"
    if schema_id is None:
        schema_id = _SCHEMAS.get(key)
        schema_source = "COMPATIBILITY_CATALOG"
    require(schema_id is not None, "INVALID_BINDING", "Output schema is not trusted")
    result_kind = manifest.get("result_kind", manifest.get("kind"))
    require(
        result_kind
        in {"OPERATOR_RESULT", "SPATIAL_VECTOR", "SPATIAL_SUMMARY", "CARTOGRAPHIC_PRODUCT"},
        "INVALID_BINDING",
        "Output result kind is not supported",
    )
    return {
        "result_kind": result_kind,
        "operator_id": key[0],
        "operator_version": key[1],
        "output_port": representation_id,
        "schema_id": schema_id,
        "schema_source": schema_source,
        "representation": representation,
    }


def require_expected(expected, facts):
    actual = {key: facts[key] for key in expected}
    require(actual == expected, "INVALID_BINDING", "Output does not satisfy its declared role")


def exact_deliverable_digest(role_bindings):
    """Role names are semantic identity, so exchanging entities changes the digest."""

    return digest({"role_bindings": role_bindings})
