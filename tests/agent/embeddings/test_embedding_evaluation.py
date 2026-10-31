import pytest
from platform_agent_runtime.embedding_evaluation import _rank, evaluate_embedding_suite
from platform_contracts.agent_runtime import parse_decision_proposal
from platform_core.application.tool_catalog import (
    build_tool_catalog,
    tool_spec,
    validate_tool_arguments,
)
from platform_core.domain.control import RuleViolation
from platform_core.domain.embeddings import EmbeddingBatch, EmbeddingRequest


def test_ranking_is_stable_on_equal_scores():
    ranked = _rank(
        (1.0, 0.0),
        [{"id": "B"}, {"id": "A"}],
        ((1.0, 0.0), (1.0, 0.0)),
    )
    assert [value["id"] for value in ranked] == ["A", "B"]


def test_suite_only_passes_when_selected_model_meets_frozen_thresholds(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(
        """{
          "suite_id":"test-v1",
          "cards":[{"id":"A","text":"alpha"},{"id":"B","text":"beta"}],
          "queries":[{"id":"A","text":"alpha query"},{"id":"B","text":"beta query"}],
          "selected_model_thresholds":{
            "top1_accuracy":1.0,"recall_at_3":1.0,
            "mean_reciprocal_rank":1.0,"single_batch_cosine":0.999
          }
        }"""
    )

    class Client:
        def __init__(self, _base_url, *, model, dimensions):
            self.model = model
            self.dimensions = dimensions

        def embed(self, request: EmbeddingRequest):
            vectors = tuple(
                (1.0, 0.0) if "alpha" in value else (0.0, 1.0) for value in request.inputs
            )
            return EmbeddingBatch(self.model, self.dimensions, vectors, 1, {})

    report = evaluate_embedding_suite(
        "http://example.test",
        suite,
        ["selected"],
        selected_model="selected",
        dimensions=2,
        client_factory=Client,
    )
    assert report["status"] == "PASS"
    assert report["authorization_effect"] == "NONE"


def test_embedding_candidates_cannot_become_authority_or_tool_arguments():
    with pytest.raises(ValueError):
        parse_decision_proposal(
            {
                "kind": "EXPLANATION",
                "frame_id": "frame-1",
                "expected_task_revision": 1,
                "message": "candidate only",
                "cited_refs": [],
                "embedding_candidates": ["execution.submit_workflow"],
            }
        )
    catalog = build_tool_catalog("science")
    spec = tool_spec(catalog, "execution.submit_workflow", "1.0.0")
    with pytest.raises(RuleViolation):
        validate_tool_arguments(
            spec,
            {
                "package_id": "p3-timeseries-v1",
                "embedding_score": 0.99,
            },
        )
