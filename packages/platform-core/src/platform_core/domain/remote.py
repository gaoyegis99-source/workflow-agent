"""Standard-library validation for privileged remote-service registry writes."""

import hashlib
import json
import math
import re
from urllib.parse import urlsplit

from platform_core.domain.control import require

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
PROTOCOLS = {"v7-remote-execution-v1", "v7-remote-execution-v2"}


def validate_remote_binding(value):
    require(isinstance(value, dict), "MALFORMED_REQUEST", "Invalid remote binding", 400)
    required = {"service_binding_id", "protocol", "endpoint", "credential_ref", "targets", "limits"}
    require(
        required <= set(value) <= required | {"tls_ca_ref"}
        and isinstance(value.get("service_binding_id"), str)
        and IDENTIFIER.fullmatch(value["service_binding_id"])
        and value.get("protocol") in PROTOCOLS
        and isinstance(value.get("credential_ref"), str)
        and IDENTIFIER.fullmatch(value["credential_ref"]),
        "MALFORMED_REQUEST",
        "Invalid remote binding",
        400,
    )
    parsed = urlsplit(value.get("endpoint", ""))
    require(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and not parsed.username
        and not parsed.password,
        "MALFORMED_REQUEST",
        "Invalid remote endpoint",
        400,
    )
    v2 = value["protocol"].endswith("v2")
    ca = value.get("tls_ca_ref")
    require(
        (not v2 or parsed.scheme == "https")
        and ((isinstance(ca, str) and IDENTIFIER.fullmatch(ca)) if v2 else ca is None),
        "MALFORMED_REQUEST",
        "Remote v2 requires HTTPS and a CA reference",
        400,
    )
    limits = value.get("limits")
    require(
        isinstance(limits, dict)
        and set(limits) == {"input_bytes", "output_bytes", "timeout_seconds"}
        and type(limits["input_bytes"]) is int
        and 0 < limits["input_bytes"] <= 20 * 1024 * 1024
        and type(limits["output_bytes"]) is int
        and 0 < limits["output_bytes"] <= 100 * 1024 * 1024
        and type(limits["timeout_seconds"]) is int
        and 0 < limits["timeout_seconds"] <= 180,
        "MALFORMED_REQUEST",
        "Invalid remote limits",
        400,
    )
    targets = value.get("targets")
    require(
        isinstance(targets, list) and 0 < len(targets) <= 32,
        "MALFORMED_REQUEST",
        "Invalid remote targets",
        400,
    )
    seen = set()
    for target in targets:
        keys = {"operator_id", "version_id", "remote_implementation_id", "contract_digest"}
        if v2:
            keys.add("implementation_digest")
        require(
            isinstance(target, dict)
            and set(target) == keys
            and target.get("operator_id") == "spatial-vector-summary"
            and target.get("version_id") == "1.0.0"
            and isinstance(target.get("remote_implementation_id"), str)
            and IDENTIFIER.fullmatch(target["remote_implementation_id"])
            and isinstance(target.get("contract_digest"), str)
            and DIGEST.fullmatch(target["contract_digest"])
            and (
                not v2
                or isinstance(target.get("implementation_digest"), str)
                and DIGEST.fullmatch(target["implementation_digest"])
            ),
            "MALFORMED_REQUEST",
            "Invalid remote target",
            400,
        )
        identity = (target["operator_id"], target["version_id"])
        require(identity not in seen, "MALFORMED_REQUEST", "Duplicate remote target", 400)
        seen.add(identity)
    # JSON canonicalization also rejects NaN/Infinity before an immutable revision is stored.
    require(
        not any(isinstance(v, float) and not math.isfinite(v) for v in limits.values()),
        "MALFORMED_REQUEST",
        "Invalid remote limits",
        400,
    )
    normalized = dict(value)
    normalized["endpoint"] = value["endpoint"].rstrip("/")
    return normalized


def remote_binding_digest(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )
