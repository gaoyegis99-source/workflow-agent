"""Framework-free model identities, usage and conservative reservation rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil
from typing import Any

from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.control import require, valid_identifier

MODEL_OBSERVATION_STATES = frozenset(
    {"ACCEPTED", "COMPLETED", "REJECTED", "RETRYABLE", "UNKNOWN", "ERROR", "CANCELLED"}
)


def sha256_digest(value: object) -> str:
    require(isinstance(value, dict), "INVALID_BINDING", "Digest input must be an object", 422)
    return canonical_digest(value)


def validate_digest(value: object, name: str) -> str:
    require(
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:]),
        "INVALID_BINDING",
        f"Invalid {name} digest",
        422,
    )
    return value


@dataclass(frozen=True, slots=True)
class ModelBudgetLimits:
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_reasoning_tokens: int | None
    max_estimated_cost_microunits: int
    max_agent_wall_seconds: int
    max_decision_frames: int

    @classmethod
    def from_record(cls, value: dict[str, Any]) -> ModelBudgetLimits:
        required = {
            "max_model_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_reasoning_tokens",
            "max_estimated_cost_microunits",
            "max_agent_wall_seconds",
            "max_decision_frames",
        }
        require(set(value) == required, "MALFORMED_REQUEST", "Invalid model budget fields", 400)
        result = cls(**value)
        positive = (
            result.max_model_calls,
            result.max_input_tokens,
            result.max_output_tokens,
            result.max_estimated_cost_microunits,
            result.max_agent_wall_seconds,
            result.max_decision_frames,
        )
        require(
            all(type(item) is int and item > 0 for item in positive)
            and (
                result.max_reasoning_tokens is None
                or type(result.max_reasoning_tokens) is int
                and result.max_reasoning_tokens >= 0
            ),
            "MALFORMED_REQUEST",
            "Model budget limits must be positive integers",
            400,
        )
        return result

    def as_record(self) -> dict[str, int | None]:
        return {
            field: getattr(self, field)
            for field in (
                "max_model_calls",
                "max_input_tokens",
                "max_output_tokens",
                "max_reasoning_tokens",
                "max_estimated_cost_microunits",
                "max_agent_wall_seconds",
                "max_decision_frames",
            )
        }


@dataclass(frozen=True, slots=True)
class ModelReservationEstimate:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_microunits: int

    def __post_init__(self) -> None:
        require(
            all(type(item) is int and item >= 0 for item in self.as_record().values()),
            "INVALID_BINDING",
            "Invalid model reservation estimate",
            422,
        )

    def as_record(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_microunits": self.cost_microunits,
        }


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_microunits: int

    @classmethod
    def from_record(cls, value: dict[str, Any]) -> ModelUsage:
        require(
            set(value)
            == {
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cost_microunits",
            },
            "INVALID_BINDING",
            "Incomplete model usage",
            422,
        )
        result = cls(**value)
        require(
            all(type(item) is int and item >= 0 for item in result.as_record().values()),
            "INVALID_BINDING",
            "Invalid model usage",
            422,
        )
        return result

    def as_record(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_microunits": self.cost_microunits,
        }


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    source_event_id: str
    model_call_id: str
    provider_revision: int
    status: str
    provider_ref: str | None
    executed: bool | None
    response: dict[str, Any] | None
    usage: dict[str, int] | None
    error_code: str | None = None

    def __post_init__(self) -> None:
        require(
            valid_identifier(self.source_event_id) and valid_identifier(self.model_call_id),
            "INVALID_BINDING",
            "Invalid model observation identity",
            422,
        )
        require(
            type(self.provider_revision) is int and self.provider_revision > 0,
            "INVALID_BINDING",
            "Invalid model observation revision",
            422,
        )
        require(
            self.status in MODEL_OBSERVATION_STATES,
            "INVALID_BINDING",
            "Invalid model observation state",
            422,
        )
        if self.provider_ref is not None:
            require(
                valid_identifier(self.provider_ref),
                "INVALID_BINDING",
                "Invalid provider response identity",
                422,
            )
        if self.usage is not None:
            ModelUsage.from_record(self.usage)
        if self.status == "COMPLETED":
            require(
                self.executed is True and self.response is not None,
                "INVALID_BINDING",
                "Incomplete model completion",
                422,
            )
        if self.status in {"REJECTED", "CANCELLED"} and self.executed is False:
            require(
                self.response is None,
                "INVALID_BINDING",
                "Unexecuted model call has a response",
                422,
            )
        if self.response is not None:
            canonical_digest(self.response)

    @property
    def response_digest(self) -> str | None:
        return canonical_digest(self.response) if self.response is not None else None

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "model_call_id": self.model_call_id,
                "provider_revision": self.provider_revision,
                "status": self.status,
                "provider_ref": self.provider_ref,
                "executed": self.executed,
                "response_digest": self.response_digest,
                "usage": self.usage,
                "error_code": self.error_code,
            }
        )


def estimate_request(request: dict[str, Any], profile: dict[str, Any]) -> ModelReservationEstimate:
    """Conservative deterministic estimate; provider usage later replaces it."""
    encoded = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    input_tokens = max(1, ceil(len(encoded) / 3))
    output_tokens = profile["max_output_tokens"]
    reasoning_tokens = profile["max_reasoning_tokens"]
    pricing = profile["pricing"]
    cost = ceil(input_tokens * pricing["input_microunits_per_million"] / 1_000_000)
    cost += ceil(output_tokens * pricing["output_microunits_per_million"] / 1_000_000)
    cost += ceil(reasoning_tokens * pricing["reasoning_microunits_per_million"] / 1_000_000)
    return ModelReservationEstimate(input_tokens, output_tokens, reasoning_tokens, cost)
