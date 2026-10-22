"""Frozen, trusted Toil runtime profiles for workflow root executions."""

import hashlib
import json

OFF_PROFILE_ID = "toil-single-host-v2-chaining-off"
ON_PROFILE_ID = "toil-single-host-v2-chaining-on"

# R5 changes the selected value only after the candidate passes every gate.
SELECTED_PROFILE_ID = OFF_PROFILE_ID

_REQUIRED = {
    "schema_version",
    "profile_id",
    "toil_version",
    "disable_chaining",
    "retry_count",
    "max_cores",
    "max_jobs",
    "max_local_jobs",
    "default_memory",
    "profile_digest",
}


def _unsigned(profile_id, disable_chaining):
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "toil_version": "9.5.0",
        "disable_chaining": disable_chaining,
        "retry_count": 0,
        "max_cores": 2,
        "max_jobs": 10,
        "max_local_jobs": 10,
        "default_memory": "256Mi",
    }


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _make(profile_id, disable_chaining):
    value = _unsigned(profile_id, disable_chaining)
    return value | {"profile_digest": _digest(value)}


_PROFILES = {
    OFF_PROFILE_ID: _make(OFF_PROFILE_ID, True),
    ON_PROFILE_ID: _make(ON_PROFILE_ID, False),
}


def profile(profile_id=SELECTED_PROFILE_ID):
    """Return a detached copy of one exact allowlisted profile."""
    if profile_id not in _PROFILES:
        raise ValueError("Unknown workflow engine runtime profile")
    return dict(_PROFILES[profile_id])


def validate(value):
    """Reject partial, extended or altered profiles before constructing CLI arguments."""
    if not isinstance(value, dict) or set(value) != _REQUIRED:
        raise ValueError("Invalid workflow engine runtime profile")
    expected = _PROFILES.get(value.get("profile_id"))
    if expected is None or value != expected:
        raise ValueError("Workflow engine runtime profile differs from its allowlisted definition")
    return dict(expected)


def diagnostic(value):
    value = validate(value)
    return {
        "profile_id": value["profile_id"],
        "profile_digest": value["profile_digest"],
        "chaining_enabled": not value["disable_chaining"],
    }
