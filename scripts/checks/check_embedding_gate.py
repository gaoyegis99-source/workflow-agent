#!/usr/bin/env python3
"""Fail-closed authentication, identity and latency gate for an embedding endpoint."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from platform_agent_runtime.embedding_client import (
    EmbeddingServiceError,
    _read_credential,
    embedding_client_from_catalog,
)
from platform_core.application.embedding_catalog import load_embedding_catalog
from platform_core.domain.embeddings import EmbeddingRequest


def rejected(client, request: EmbeddingRequest) -> bool:
    try:
        client.embed(request)
    except EmbeddingServiceError as error:
        return error.status_code in {401, 403}
    return False


def run_gate(catalog: dict) -> dict:
    if catalog["state"] != "ENABLED":
        return {"status": "DISABLED", "catalog_digest": catalog["catalog_digest"]}
    token = _read_credential(catalog["credential_file"])
    request = EmbeddingRequest(
        catalog["model_id"], ("groundwater embedding production gate",), catalog["dimensions"]
    )
    if not rejected(embedding_client_from_catalog(catalog, None), request):
        raise RuntimeError("Embedding endpoint did not reject a missing credential")
    if not rejected(embedding_client_from_catalog(catalog, "v7-invalid-gate-credential"), request):
        raise RuntimeError("Embedding endpoint did not reject an incorrect credential")

    client = embedding_client_from_catalog(catalog, token)
    for _ in range(3):
        client.embed(request)
    durations = [float(client.embed(request).gateway["client_wall_ms"]) / 1000 for _ in range(20)]
    ordered = sorted(durations)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    if max(ordered) > catalog["request_timeout_seconds"] or p95 > catalog["p95_limit_seconds"]:
        raise RuntimeError("Embedding endpoint failed the frozen latency gate")
    return {
        "status": "PASSED",
        "catalog_digest": catalog["catalog_digest"],
        "warmup_count": 3,
        "measurement_count": len(ordered),
        "p95_seconds": p95,
        "max_seconds": max(ordered),
        "authentication": {"missing": "REJECTED", "incorrect": "REJECTED", "correct": "PASSED"},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_gate(load_embedding_catalog(args.catalog))
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")


if __name__ == "__main__":
    main()
