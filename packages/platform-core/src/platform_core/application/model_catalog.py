"""Deployment-owned, digest-frozen model profile catalogue."""

from __future__ import annotations

import json
from pathlib import Path

from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.control import require, valid_identifier
from platform_core.domain.model_gateway import validate_digest

PROFILE_FIELDS = {
    "profile_id",
    "provider",
    "model",
    "request_mode",
    "supports_structured_output",
    "supports_query_by_response_id",
    "data_policy",
    "max_context_tokens",
    "max_output_tokens",
    "max_reasoning_tokens",
    "timeout_seconds",
    "pricing_revision",
    "pricing",
    "credential_env",
    "base_url",
    "profile_digest",
}


def validate_profile(value: dict) -> dict:
    require(set(value) == PROFILE_FIELDS, "INVALID_BINDING", "Invalid model profile fields", 422)
    require(
        valid_identifier(value["profile_id"])
        and value["provider"] in {"OPENAI_RESPONSES", "DETERMINISTIC_FAKE", "RECORD_REPLAY"}
        and value["request_mode"] == "RESPONSES"
        and value["data_policy"] in {"LOCAL_TEST_ONLY", "PROJECT_APPROVED_REMOTE"},
        "INVALID_BINDING",
        "Invalid model profile identity",
        422,
    )
    require(
        isinstance(value["model"], str)
        and 0 < len(value["model"]) <= 200
        and isinstance(value["base_url"], str)
        and value["base_url"].startswith(("https://", "http://127.0.0.1:")),
        "INVALID_BINDING",
        "Invalid model endpoint or snapshot",
        422,
    )
    require(
        value["supports_structured_output"] is True
        and type(value["supports_query_by_response_id"]) is bool,
        "INVALID_BINDING",
        "The selected model must support strict structured output",
        422,
    )
    for field in (
        "max_context_tokens",
        "max_output_tokens",
        "max_reasoning_tokens",
        "timeout_seconds",
    ):
        require(
            type(value[field]) is int and value[field] >= 0,
            "INVALID_BINDING",
            "Invalid model profile limit",
            422,
        )
    pricing = value["pricing"]
    require(
        isinstance(pricing, dict)
        and set(pricing)
        == {
            "input_microunits_per_million",
            "output_microunits_per_million",
            "reasoning_microunits_per_million",
        }
        and all(type(item) is int and item >= 0 for item in pricing.values()),
        "INVALID_BINDING",
        "Invalid model pricing",
        422,
    )
    expected = canonical_digest(
        {key: item for key, item in value.items() if key != "profile_digest"}
    )
    require(
        validate_digest(value["profile_digest"], "model profile") == expected,
        "INVALID_BINDING",
        "Model profile digest mismatch",
        422,
    )
    return value


def build_catalog(profiles: list[dict], default_profile_id: str) -> dict:
    require(
        profiles and valid_identifier(default_profile_id),
        "INVALID_BINDING",
        "Model catalogue is empty",
        422,
    )
    validated = [validate_profile(dict(profile)) for profile in profiles]
    require(
        len({profile["profile_id"] for profile in validated}) == len(validated)
        and default_profile_id in {profile["profile_id"] for profile in validated},
        "INVALID_BINDING",
        "Model profile identities are invalid",
        422,
    )
    catalog = {
        "schema_version": 1,
        "default_profile_id": default_profile_id,
        "profiles": validated,
    }
    return catalog | {"catalog_digest": canonical_digest(catalog)}


def load_catalog(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("Model catalogue cannot be read") from error
    require(
        isinstance(value, dict)
        and set(value) == {"schema_version", "default_profile_id", "profiles", "catalog_digest"}
        and value.get("schema_version") == 1,
        "INVALID_BINDING",
        "Invalid model catalogue",
        422,
    )
    expected = build_catalog(value["profiles"], value["default_profile_id"])
    require(value == expected, "INVALID_BINDING", "Model catalogue digest mismatch", 422)
    return value


def selected_profile(catalog: dict) -> dict:
    return next(
        profile
        for profile in catalog["profiles"]
        if profile["profile_id"] == catalog["default_profile_id"]
    )
