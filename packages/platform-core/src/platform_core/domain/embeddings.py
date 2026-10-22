"""Strict, provider-neutral embedding values used by P6 diagnostics."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    model: str
    inputs: tuple[str, ...]
    dimensions: int = 1024

    def __post_init__(self) -> None:
        if not self.model or len(self.model) > 128:
            raise ValueError("Embedding model must be a non-empty bounded identifier")
        if not 1 <= len(self.inputs) <= 128:
            raise ValueError("Embedding requests must contain between 1 and 128 inputs")
        normalized = tuple(unicodedata.normalize("NFC", value) for value in self.inputs)
        if any(not value or len(value) > 32_000 for value in normalized):
            raise ValueError("Embedding inputs must be non-empty and at most 32,000 characters")
        if sum(len(value) for value in normalized) > 256_000:
            raise ValueError("Embedding request text exceeds the batch limit")
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 4096:
            raise ValueError("Embedding dimensions must be between 1 and 4096")
        object.__setattr__(self, "inputs", normalized)


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    model: str
    dimensions: int
    vectors: tuple[tuple[float, ...], ...]
    prompt_tokens: int | None
    gateway: dict[str, object]

    def __post_init__(self) -> None:
        if not self.vectors:
            raise ValueError("Embedding response must contain at least one vector")
        for vector in self.vectors:
            if len(vector) != self.dimensions:
                raise ValueError("Embedding vector dimension does not match the response")
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("Embedding vector contains a non-finite value")
            if not any(value != 0.0 for value in vector):
                raise ValueError("Embedding service returned a zero vector")


def cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("Cosine inputs must have the same non-zero dimension")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("Cosine similarity is undefined for zero vectors")
    return numerator / (left_norm * right_norm)
