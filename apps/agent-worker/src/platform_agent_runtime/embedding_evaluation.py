"""P6 semantic retrieval diagnostics; embeddings never authorize actions."""

from __future__ import annotations

import json
import time
from pathlib import Path

from platform_core.domain.embeddings import EmbeddingRequest, cosine_similarity

from platform_agent_runtime.embedding_client import HttpEmbeddingClient


def _rank(
    query: tuple[float, ...],
    cards: list[dict],
    card_vectors: tuple[tuple[float, ...], ...],
) -> list[dict]:
    return sorted(
        (
            {
                "id": card["id"],
                "score": round(cosine_similarity(query, vector), 8),
            }
            for card, vector in zip(cards, card_vectors, strict=True)
        ),
        key=lambda item: (-item["score"], item["id"]),
    )


def evaluate_embedding_model(
    base_url: str,
    suite: dict,
    model: str,
    *,
    dimensions: int = 1024,
    client_factory=HttpEmbeddingClient,
) -> dict:
    cards = suite["cards"]
    queries = suite["queries"]
    if [value["id"] for value in cards] != [value["id"] for value in queries]:
        raise ValueError("Embedding diagnostic cards and queries must have identical IDs")
    client = client_factory(base_url, model=model, dimensions=dimensions)
    started = time.monotonic()
    card_batch = client.embed(
        EmbeddingRequest(model, tuple(value["text"] for value in cards), dimensions)
    )
    query_batch = client.embed(
        EmbeddingRequest(model, tuple(value["text"] for value in queries), dimensions)
    )
    consistency_count = min(3, len(cards))
    consistency = []
    for index in range(consistency_count):
        single = client.embed(EmbeddingRequest(model, (cards[index]["text"],), dimensions))
        consistency.append(cosine_similarity(card_batch.vectors[index], single.vectors[0]))
    results = []
    reciprocal_ranks = []
    for query, vector in zip(queries, query_batch.vectors, strict=True):
        ranking = _rank(vector, cards, card_batch.vectors)
        position = next(index for index, item in enumerate(ranking, 1) if item["id"] == query["id"])
        reciprocal_ranks.append(1.0 / position)
        results.append(
            {
                "id": query["id"],
                "rank": position,
                "top3": ranking[:3],
                "passed_at_3": position <= 3,
            }
        )
    top1 = sum(value["rank"] == 1 for value in results) / len(results)
    recall3 = sum(value["rank"] <= 3 for value in results) / len(results)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
    return {
        "model": model,
        "dimensions": dimensions,
        "status": "MEASURED",
        "metrics": {
            "top1_accuracy": round(top1, 6),
            "recall_at_3": round(recall3, 6),
            "mean_reciprocal_rank": round(mrr, 6),
            "single_batch_cosine_min": round(min(consistency), 8),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "prompt_tokens": (card_batch.prompt_tokens or 0) + (query_batch.prompt_tokens or 0),
        },
        "gateway": {
            "card_batch": card_batch.gateway,
            "query_batch": query_batch.gateway,
        },
        "results": results,
    }


def evaluate_embedding_suite(
    base_url: str,
    cases_path: str | Path,
    models: list[str],
    *,
    selected_model: str = "qwen3-embedding-4b-q4",
    dimensions: int = 1024,
    client_factory=HttpEmbeddingClient,
) -> dict:
    suite = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    if selected_model not in models:
        raise ValueError("Selected embedding model must be included in the evaluation")
    results = [
        evaluate_embedding_model(
            base_url,
            suite,
            model,
            dimensions=dimensions,
            client_factory=client_factory,
        )
        for model in models
    ]
    selected = next(value for value in results if value["model"] == selected_model)
    thresholds = suite["selected_model_thresholds"]
    failures = []
    for metric, threshold in thresholds.items():
        actual_name = "single_batch_cosine_min" if metric == "single_batch_cosine" else metric
        if selected["metrics"][actual_name] < threshold:
            failures.append(
                {
                    "metric": actual_name,
                    "expected_minimum": threshold,
                    "actual": selected["metrics"][actual_name],
                }
            )
    return {
        "suite_id": suite["suite_id"],
        "selected_model": selected_model,
        "dimensions": dimensions,
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authorization_effect": "NONE",
        "models": results,
    }
