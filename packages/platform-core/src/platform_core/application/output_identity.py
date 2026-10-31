"""Exact published output identities derived from immutable manifests."""

from platform_core.application.control import digest
from platform_core.domain.control import require


def exact_output_requirements(spec):
    """Return the exact role contract for every executable acceptance profile.

    ``p3-timeseries-v1`` predates explicit role declarations, but its closed
    template already freezes the complete output identity.  Deriving the role
    contract here keeps P3 executable without accepting legacy execution-only
    deliverables or inventing identities from a compatibility catalogue.
    """

    if spec.get("profile") == "open-science-v1":
        return list(spec.get("required_outputs", []))
    if spec.get("profile") == "p3-timeseries-v1":
        return [
            {
                "role_id": operator_id,
                "necessary": True,
                "expected": {
                    "result_kind": "OPERATOR_RESULT",
                    "operator_id": operator_id,
                    "operator_version": "1.0.0",
                    "output_port": "result",
                    "schema_id": "p3-result-v1",
                },
            }
            for operator_id in spec.get("required_outputs", [])
        ]
    return []


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
    schema_id = representation.get("schema_id")
    require(
        isinstance(schema_id, str) and bool(schema_id),
        "INVALID_BINDING",
        "Output schema must be declared by the immutable manifest",
    )
    result_kind = manifest.get("result_kind", manifest.get("kind"))
    require(
        result_kind
        in {"OPERATOR_RESULT", "SPATIAL_VECTOR", "SPATIAL_SUMMARY", "CARTOGRAPHIC_PRODUCT"},
        "INVALID_BINDING",
        "Output result kind is not supported",
    )
    return {
        "result_kind": result_kind,
        "operator_id": target.get("operator_id"),
        "operator_version": target.get("version_id"),
        "output_port": representation_id,
        "schema_id": schema_id,
        "schema_source": "MANIFEST",
        "representation": representation,
    }


def require_expected(expected, facts):
    actual = {key: facts[key] for key in expected}
    require(actual == expected, "INVALID_BINDING", "Output does not satisfy its declared role")


def exact_deliverable_digest(role_bindings):
    """Role names are semantic identity, so exchanging entities changes the digest."""

    return digest({"role_bindings": role_bindings})
