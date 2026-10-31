import pytest
from platform_contracts.knowledge import SemanticIndexProfile
from platform_contracts.service import ServiceFault
from platform_knowledge.semantic import (
    SemanticDiscovery,
    SemanticIndexer,
    fuse_candidates,
    profile_digest,
)


class FakeEncoder:
    def embed(self, texts):
        values = []
        for text in texts:
            # Deterministic two-dimensional test vectors; no model or network.
            values.append((1.0, 0.0) if "head" in text.casefold() else (0.0, 1.0))
        return values


class FakeRepository:
    def __init__(self):
        self.indexes = {}
        self.vectors = []
        self.units = [
            {
                "document_id": "document-a",
                "version_id": "1",
                "unit_id": "unit-a",
                "text": "Groundwater head observation",
                "content_digest": "sha256:" + "a" * 64,
                "lifecycle_revision": 1,
            },
            {
                "document_id": "document-b",
                "version_id": "1",
                "unit_id": "unit-b",
                "text": "Unrelated rainfall observation",
                "content_digest": "sha256:" + "b" * 64,
                "lifecycle_revision": 1,
            },
        ]

    def begin_semantic_index(self, project, index_id, profile, profile_digest):
        self.indexes[index_id] = {
            "project_id": project,
            "profile": profile,
            "profile_digest": profile_digest,
            "status": "BUILDING",
        }

    def active_units_for_semantic_index(self, project):
        assert project == "project"
        return list(self.units)

    def replace_semantic_vectors(self, project, index_id, profile_digest, records):
        self.vectors = list(records)
        self.indexes[index_id]["status"] = "READY"

    def fail_semantic_index(self, project, index_id, profile_digest, category):
        self.indexes[index_id]["status"] = "FAILED"

    def stale_semantic_index(self, project, index_id, profile_digest):
        self.indexes[index_id]["status"] = "STALE"

    def semantic_index(self, project, index_id):
        row = self.indexes.get(index_id)
        return dict(row) if row and row["project_id"] == project else None

    def semantic_candidates(self, project, index_id, profile_digest, *, sources=None):
        if sources == []:
            return []
        records = []
        for vector in self.vectors:
            unit = next(value for value in self.units if value["unit_id"] == vector["unit_id"])
            records.append(
                {
                    **vector,
                    "text": unit["text"],
                    "title": vector["document_id"],
                    "start_char": 0,
                    "end_char": len(unit["text"]),
                    "availability": "ACTIVE",
                    "epistemic_status": "DECLARED",
                    "review_state": "UNREVIEWED",
                }
            )
        return records


def profile():
    return SemanticIndexProfile(
        embedding_model_id="test-embedding",
        embedding_model_digest="sha256:" + "c" * 64,
        dimensions=2,
        chunker_id="paragraph-window-v1",
        chunker_digest="sha256:" + "d" * 64,
        document_projection_version="v1",
    )


def request(index="semantic-v1"):
    return {
        "query": "groundwater head",
        "limit": 20,
        "index": {"index_id": index, "profile_digest": profile_digest(profile())},
    }


def test_semantic_index_is_versioned_candidate_discovery_not_evidence():
    repo = FakeRepository()
    SemanticIndexer(repo, FakeEncoder()).rebuild("project", "semantic-v1", profile())

    result = SemanticDiscovery(repo, FakeEncoder()).search("project", request())
    assert result["status"] == "RESULTS"
    assert result["items"][0]["unit_id"] == "unit-a"
    assert result["items"][0]["discovery_kind"] == "SEMANTIC_CANDIDATE"
    assert result["items"][0]["exact_read_required"] is True
    assert "evidence_ref" not in result["items"][0]


def test_semantic_discovery_fails_closed_for_stale_or_missing_encoder():
    repo = FakeRepository()
    SemanticIndexer(repo, FakeEncoder()).rebuild("project", "semantic-v1", profile())
    repo.indexes["semantic-v1"]["status"] = "STALE"
    stale = SemanticDiscovery(repo, FakeEncoder()).search("project", request())
    assert stale["status"] == "INDEX_UNAVAILABLE"
    assert stale["reason"] == "INDEX_STALE"

    repo.indexes["semantic-v1"]["status"] = "READY"
    unavailable = SemanticDiscovery(repo).search("project", request())
    assert unavailable["status"] == "INDEX_UNAVAILABLE"
    assert unavailable["reason"] == "ENCODER_UNAVAILABLE"


def test_profile_and_vectors_must_match_the_declared_identity():
    repo = FakeRepository()
    with pytest.raises(ServiceFault, match="Embedding dimensions"):

        class BadEncoder:
            def embed(self, texts):
                return [(1.0,)] * len(texts)

        SemanticIndexer(repo, BadEncoder()).rebuild("project", "semantic-v1", profile())


def test_concurrent_corpus_change_leaves_index_stale_not_ready():
    class ChangingRepository(FakeRepository):
        def replace_semantic_vectors(self, project, index_id, profile_digest, records):
            raise ServiceFault("REVISION_CONFLICT", "Knowledge units changed while indexing", 409)

    repo = ChangingRepository()
    with pytest.raises(ServiceFault, match="Knowledge units changed"):
        SemanticIndexer(repo, FakeEncoder()).rebuild("project", "semantic-v1", profile())
    assert repo.indexes["semantic-v1"]["status"] == "STALE"


def test_fusion_preserves_channel_provenance_and_exact_read_requirement():
    lexical = [
        {
            "document_id": "document-a",
            "version_id": "1",
            "unit_id": "unit-a",
            "title": "A",
            "snippet": "head",
            "start_char": 0,
            "end_char": 4,
            "availability": "ACTIVE",
            "epistemic_status": "DECLARED",
            "review_state": "UNREVIEWED",
        }
    ]
    semantic = [
        {**lexical[0], "discovery_kind": "SEMANTIC_CANDIDATE"},
        {
            **lexical[0],
            "document_id": "document-b",
            "unit_id": "unit-b",
            "title": "B",
        },
    ]
    rows = fuse_candidates(lexical, semantic, 20)
    assert rows[0]["discovery_channels"] == ["LEXICAL", "SEMANTIC"]
    assert all(row["exact_read_required"] for row in rows)
