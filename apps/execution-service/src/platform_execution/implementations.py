"""Closed and disjoint compute/checker implementation registries."""

COMPUTE_IMPLEMENTATIONS = {
    "p3-completeness-v1": {
        "operator_id": "timeseries-completeness",
        "image_key": "OPERATOR",
        "protocol": "P3_TABLE",
        "resource_profile": "native-small-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"result": "result.json", "table": "result.parquet", "method": "method.json"},
    },
    "p3-numpy-v1": {
        "operator_id": "statistics-linear-trend",
        "image_key": "OPERATOR",
        "protocol": "P3_TABLE",
        "resource_profile": "native-small-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"result": "result.json", "table": "result.parquet", "method": "method.json"},
    },
    "qgis-buffer-v1": {
        "operator_id": "qgis-buffer",
        "image_key": "QGIS",
        "protocol": "SPATIAL_VECTOR",
        "resource_profile": "qgis-medium-v1",
        "cpus": "1",
        "memory": "2g",
        "pids": "128",
        "timeout_seconds": 180,
        "outputs": {"vector": "buffer.gpkg", "manifest": "manifest.json", "method": "method.json"},
    },
    "spatial-vector-summary-v1": {
        "operator_id": "spatial-vector-summary",
        "image_key": "SPATIAL_NATIVE",
        "protocol": "SPATIAL_SUMMARY",
        "resource_profile": "spatial-native-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "128",
        "timeout_seconds": 120,
        "outputs": {"summary": "summary.json", "method": "method.json"},
    },
    "qgis-static-map-v1": {
        "operator_id": "qgis-static-map",
        "image_key": "QGIS",
        "protocol": "CARTOGRAPHIC_PNG",
        "resource_profile": "qgis-medium-v1",
        "cpus": "1",
        "memory": "2g",
        "pids": "128",
        "timeout_seconds": 180,
        "outputs": {"image": "map.png", "manifest": "manifest.json", "method": "method.json"},
    },
    "table-profile-native-v1": {
        "operator_id": "table-profile",
        "image_key": "TABULAR",
        "protocol": "TABULAR_V1",
        "resource_profile": "table-small-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"report": "report.json", "table": "table.parquet", "method": "method.json"},
    },
    "table-validate-native-v1": {
        "operator_id": "table-validate",
        "image_key": "TABULAR",
        "protocol": "TABULAR_V1",
        "resource_profile": "table-small-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"report": "report.json", "table": "table.parquet", "method": "method.json"},
    },
    "table-project-native-v1": {
        "operator_id": "table-project",
        "image_key": "TABULAR",
        "protocol": "TABULAR_V1",
        "resource_profile": "table-small-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"report": "report.json", "table": "table.parquet", "method": "method.json"},
    },
    "table-filter-native-v1": {
        "operator_id": "table-filter",
        "image_key": "TABULAR",
        "protocol": "TABULAR_V1",
        "resource_profile": "table-small-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"report": "report.json", "table": "table.parquet", "method": "method.json"},
    },
    "table-cast-native-v1": {
        "operator_id": "table-cast",
        "image_key": "TABULAR",
        "protocol": "TABULAR_V1",
        "resource_profile": "table-small-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"report": "report.json", "table": "table.parquet", "method": "method.json"},
    },
    "table-aggregate-native-v1": {
        "operator_id": "table-aggregate",
        "image_key": "TABULAR",
        "protocol": "TABULAR_V1",
        "resource_profile": "table-small-v1",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"report": "report.json", "table": "table.parquet", "method": "method.json"},
    },
}

CHECKER_IMPLEMENTATIONS = {
    "p3-decimal-v1": {
        "operator_id": "p3-timeseries-check",
        "image_key": "CHECK",
        "protocol": "P3_CHECK",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"check": "check.json"},
    }
}
for identity, image_key in {
    "platform-runtime-integrity-v1": "CHECK",
    "p3-timeseries-reproduction-v1": "CHECK",
    "spatial-vector-validity-v1": "GDAL_CHECKER",
    "cartographic-product-integrity-v1": "CHECK",
    "p3-descriptive-claim-support-v1": "CHECK",
    "tabular-profile-reproduction-v1": "CHECK",
    "tabular-validation-reproduction-v1": "CHECK",
    "tabular-project-reproduction-v1": "CHECK",
    "tabular-filter-reproduction-v1": "CHECK",
    "tabular-cast-reproduction-v1": "CHECK",
    "tabular-aggregate-reproduction-v1": "CHECK",
}.items():
    CHECKER_IMPLEMENTATIONS[identity] = {
        "operator_id": "open-science-check",
        "image_key": image_key,
        "protocol": "CAPABILITY_CHECK",
        "cpus": "1",
        "memory": "1g",
        "pids": "64",
        "timeout_seconds": 120,
        "outputs": {"check": "check.json"},
    }

REMOTE_SPATIAL_SUMMARY_CONTRACT = (
    "sha256:8a49ec81ca22f0ab595ea30cc8f1f9641433ee9820e9a76e095d26889b0754ee"
)


def remote_implementation(payload):
    target = payload.get("target", {})
    binding = target.get("execution_binding", {})
    frozen = payload.get("_remote_binding", {})
    allowed = frozen.get("target", {})
    if not (
        target.get("operator_id") == "spatial-vector-summary"
        and target.get("version_id") == "1.0.0"
        and binding.get("kind") == "REMOTE"
        and frozen.get("protocol") in {"v7-remote-execution-v1", "v7-remote-execution-v2"}
        and frozen.get("service_binding_id") == binding.get("service_binding_id")
        and frozen.get("revision") == binding.get("binding_revision")
        and allowed.get("operator_id") == target.get("operator_id")
        and allowed.get("version_id") == target.get("version_id")
        and allowed.get("contract_digest") == REMOTE_SPATIAL_SUMMARY_CONTRACT
        and (
            frozen.get("protocol") == "v7-remote-execution-v1"
            or isinstance(allowed.get("implementation_digest"), str)
            and allowed["implementation_digest"].startswith("sha256:")
            and len(allowed["implementation_digest"]) == 71
        )
    ):
        raise ValueError("Remote binding is not registered for this exact contract")
    local = COMPUTE_IMPLEMENTATIONS["spatial-vector-summary-v1"]
    return {
        **local,
        "implementation_id": allowed["remote_implementation_id"],
        "runtime_kind": "REMOTE",
        "binding_digest": frozen["binding_digest"],
        "image_id": allowed.get("implementation_digest", frozen["binding_digest"]),
        "timeout_seconds": frozen["limits"]["timeout_seconds"],
    }


def implementation(payload):
    target = payload.get("target", {})
    binding = target.get("execution_binding", {})
    identity = binding.get("implementation_id")
    kind = payload.get("kind", "OPERATOR")
    registry = CHECKER_IMPLEMENTATIONS if kind == "CHECK" else COMPUTE_IMPLEMENTATIONS
    spec = registry.get(identity)
    if not (
        kind in {"OPERATOR", "CHECK"}
        and spec is not None
        and target.get("operator_id") == spec["operator_id"]
        and target.get("version_id") == "1.0.0"
        and binding == {"kind": "LOCAL", "implementation_id": identity}
    ):
        raise ValueError("Only registered implementations are accepted")
    if kind == "OPERATOR" and payload.get("_operator_snapshot") is not None:
        frozen = payload["_operator_snapshot"]
        executable = frozen.get("implementation") or {}
        if not (
            frozen.get("operator_id") == target["operator_id"]
            and frozen.get("version_id") == target["version_id"]
            and executable.get("implementation_id") == identity
            and executable.get("protocol") == spec["protocol"]
            and executable.get("resource_profile") == spec["resource_profile"]
        ):
            raise ValueError("Frozen operator catalogue entry differs from the runtime")
    return identity, spec
