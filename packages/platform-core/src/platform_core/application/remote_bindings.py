"""Resolve immutable, project-scoped remote service bindings for execution."""

from platform_core.domain.control import require


def freeze_remote_binding(uow, project: str, target: dict) -> dict:
    binding = target.get("execution_binding", {})
    require(
        target.get("kind") == "OPERATOR"
        and target.get("operator_id") == "spatial-vector-summary"
        and target.get("version_id") == "1.0.0"
        and binding.get("kind") == "REMOTE",
        "UNSUPPORTED_TARGET",
        "Only the registered remote spatial summary is supported",
        422,
    )
    record = uow.service_bindings.get(
        project, binding["service_binding_id"], binding["binding_revision"]
    )
    require(
        record["enabled"],
        "PRECONDITION_UNRESOLVED",
        "Remote service binding is disabled",
        409,
    )
    match = next(
        (
            item
            for item in record["targets"]
            if item["operator_id"] == target["operator_id"]
            and item["version_id"] == target["version_id"]
        ),
        None,
    )
    require(
        match is not None,
        "UNSUPPORTED_TARGET",
        "Remote binding does not allow this target",
        422,
    )
    return {
        "service_binding_id": record["service_binding_id"],
        "revision": record["revision"],
        "protocol": record["protocol"],
        "endpoint": record["endpoint"],
        "credential_ref": record["credential_ref"],
        "tls_ca_ref": record.get("tls_ca_ref"),
        "limits": record["limits"],
        "binding_digest": record["binding_digest"],
        "target": match,
    }
