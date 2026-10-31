"""Versioned semantic candidate discovery for Knowledge.

Semantic vectors are deliberately a discovery aid.  This module never emits an
``EvidenceRef`` or authorises a project action: callers must use the existing
exact-read operation before citing, accepting, budgeting, or executing work.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from platform_contracts.knowledge import SemanticIndexProfile
from platform_contracts.service import ServiceFault, digest, ensure


class EmbeddingPort(Protocol):
    """Trusted model adapter; its configuration is owned by composition, not here."""

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


def profile_digest(profile: SemanticIndexProfile | Mapping[str, object]) -> str:
    value = (
        profile.model_dump(mode="json")
        if isinstance(profile, SemanticIndexProfile)
        else dict(profile)
    )
    return "sha256:" + digest(value)


def normalize_vector(value: Sequence[float], dimensions: int) -> tuple[float, ...]:
    ensure(
        len(value) == dimensions,
        "INVALID_BINDING",
        "Embedding dimensions differ from the semantic index profile",
        422,
    )
    numbers = []
    for item in value:
        ensure(
            type(item) in {int, float} and math.isfinite(float(item)),
            "INVALID_BINDING",
            "Embedding contains a non-finite numeric value",
            422,
        )
        numbers.append(float(item))
    norm = math.sqrt(sum(item * item for item in numbers))
    ensure(norm > 0.0, "INVALID_BINDING", "Embedding must not be a zero vector", 422)
    return tuple(item / norm for item in numbers)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    ensure(len(left) == len(right), "INVALID_BINDING", "Embedding dimensions differ", 422)
    return sum(a * b for a, b in zip(left, right, strict=True))


class SemanticIndexer:
    """Build an immutable-profile index from current active Knowledge units.

    The adapter sees text only during a deliberate indexing run.  Persisted
    entries contain vectors and identity metadata, while the repository rechecks
    the document lifecycle before making a completed build visible.
    """

    def __init__(self, repository, encoder: EmbeddingPort):
        self.repo = repository
        self.encoder = encoder

    def rebuild(self, project: str, index_id: str, profile: SemanticIndexProfile) -> dict:
        identity = profile_digest(profile)
        self.repo.begin_semantic_index(project, index_id, profile.model_dump(mode="json"), identity)
        try:
            units = self.repo.active_units_for_semantic_index(project)
            vectors = self.encoder.embed([row["text"] for row in units]) if units else []
            ensure(
                len(vectors) == len(units),
                "DEPENDENCY_UNAVAILABLE",
                "Embedding adapter returned an incomplete response",
                503,
            )
            records = []
            for row, vector in zip(units, vectors, strict=True):
                records.append(
                    {
                        **{
                            key: row[key]
                            for key in (
                                "document_id",
                                "version_id",
                                "unit_id",
                                "content_digest",
                                "lifecycle_revision",
                            )
                        },
                        "vector": list(normalize_vector(vector, profile.dimensions)),
                    }
                )
            self.repo.replace_semantic_vectors(project, index_id, identity, records)
            return self.repo.semantic_index(project, index_id)
        except ServiceFault as exc:
            # A concurrently imported, updated, or withdrawn unit is not an
            # embedding-provider failure.  Keep the index explicitly stale so
            # it cannot become visible until a clean rebuild sees one frozen
            # active-unit set.
            if exc.code == "REVISION_CONFLICT":
                self.repo.stale_semantic_index(project, index_id, identity)
            else:
                self.repo.fail_semantic_index(project, index_id, identity, type(exc).__name__)
            raise
        except Exception as exc:
            # Do not expose an upstream exception's contents in the index
            # record.  Callers receive the normal dependency status instead.
            self.repo.fail_semantic_index(project, index_id, identity, type(exc).__name__)
            raise


class SemanticDiscovery:
    def __init__(self, repository, encoder: EmbeddingPort | None = None):
        self.repo = repository
        self.encoder = encoder

    @staticmethod
    def _unavailable(request: Mapping[str, object], reason: str, profile=None) -> dict:
        return {
            "status": "INDEX_UNAVAILABLE",
            "query_digest": "sha256:" + digest(request["query"]),
            "index": request["index"],
            "profile": profile,
            "reason": reason,
            "candidate_count": 0,
            "items": [],
        }

    def search(self, project: str, request: Mapping[str, object], *, sources=None) -> dict:
        requested = request["index"]
        index = self.repo.semantic_index(project, requested["index_id"])
        if index is None:
            return self._unavailable(request, "INDEX_NOT_FOUND")
        profile = SemanticIndexProfile.model_validate(index["profile"])
        if requested["profile_digest"] != index["profile_digest"]:
            return self._unavailable(request, "PROFILE_MISMATCH", profile.model_dump(mode="json"))
        if index["status"] != "READY":
            reason = "INDEX_STALE" if index["status"] == "STALE" else "NO_ACTIVE_VECTORS"
            return self._unavailable(request, reason, profile.model_dump(mode="json"))
        if self.encoder is None:
            return self._unavailable(
                request, "ENCODER_UNAVAILABLE", profile.model_dump(mode="json")
            )
        try:
            response = self.encoder.embed([request["query"]])
            ensure(len(response) == 1, "DEPENDENCY_UNAVAILABLE", "Embedding unavailable", 503)
            query = normalize_vector(response[0], profile.dimensions)
        except ServiceFault:
            return self._unavailable(
                request, "ENCODER_UNAVAILABLE", profile.model_dump(mode="json")
            )
        except Exception:
            return self._unavailable(
                request, "ENCODER_UNAVAILABLE", profile.model_dump(mode="json")
            )
        rows = self.repo.semantic_candidates(
            project,
            requested["index_id"],
            index["profile_digest"],
            sources=sources,
        )
        candidates = []
        for row in rows:
            score = cosine(query, normalize_vector(row["vector"], profile.dimensions))
            text = row["text"]
            candidates.append(
                {
                    "document_id": row["document_id"],
                    "version_id": row["version_id"],
                    "unit_id": row["unit_id"],
                    "title": row["title"],
                    "snippet": text[:600],
                    "start_char": row["start_char"],
                    "end_char": row["end_char"],
                    "score": score,
                    "availability": row["availability"],
                    "epistemic_status": row["epistemic_status"],
                    "review_state": row["review_state"],
                    "discovery_kind": "SEMANTIC_CANDIDATE",
                    "exact_read_required": True,
                }
            )
        candidates.sort(key=lambda row: (-row["score"], row["unit_id"]))
        limited = candidates[: request["limit"]]
        return {
            "status": "RESULTS" if limited else "NO_RESULTS",
            "query_digest": "sha256:" + digest(request["query"]),
            "index": requested,
            "profile": profile.model_dump(mode="json"),
            "reason": "READY" if limited else "NO_ACTIVE_VECTORS",
            "candidate_count": len(candidates),
            "items": limited,
        }


def fuse_candidates(
    lexical: Iterable[Mapping[str, object]], semantic: Iterable[Mapping[str, object]], limit: int
) -> list[dict]:
    """Fuse ranked discovery lists with RRF; the result is still not evidence."""

    merged: dict[tuple[str, str, str], dict] = {}
    for channel, rows in (("LEXICAL", lexical), ("SEMANTIC", semantic)):
        for rank, row in enumerate(rows, start=1):
            key = (row["document_id"], row["version_id"], row["unit_id"])
            target = merged.setdefault(
                key,
                {
                    key_name: row[key_name]
                    for key_name in (
                        "document_id",
                        "version_id",
                        "unit_id",
                        "title",
                        "snippet",
                        "start_char",
                        "end_char",
                        "availability",
                        "epistemic_status",
                        "review_state",
                    )
                }
                | {"score": 0.0, "discovery_channels": [], "exact_read_required": True},
            )
            target["score"] += 1.0 / (60 + rank)
            target["discovery_channels"].append(channel)
    result = list(merged.values())
    for row in result:
        row["discovery_channels"].sort()
    result.sort(key=lambda row: (-row["score"], row["unit_id"]))
    return result[:limit]
