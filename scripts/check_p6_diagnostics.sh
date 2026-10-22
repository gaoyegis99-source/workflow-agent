#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

: "${PLATFORM_EMBEDDING_BASE_URL:?Set PLATFORM_EMBEDDING_BASE_URL to the embedding service origin}"
verification=.p6-runtime/verification
release=.p6-runtime/release
mkdir -p "$verification" "$release"
export UV_LINK_MODE=copy

uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q \
  tests/p60 tests/p61 tests/p62 tests/p63 tests/p64 tests/p65 tests/p66 tests/p67 \
  tests/p68 tests/p69 tests/p610 tests/contracts tests/architecture \
  tests/p52/test_contracts_and_policy.py::test_groundwater_policy_is_local_deterministic_and_does_not_block_unrelated_work \
  tests/p53/test_migrations_and_lifecycle.py::test_withdrawal_marks_current_validity_stale_without_rewriting_report \
  tests/p5r1/test_exact_candidate.py::test_direct_output_is_canonicalized_and_saved_as_exact \
  --tb=short --junitxml="$verification/p610-simulated-tests.xml"

uv run --locked python scripts/run_p6_golden.py \
  --junit "$verification/p610-golden-tests.xml" --repeats 2
uv run --locked python scripts/evaluate_p6.py --profile deterministic \
  --junit "$verification/p610-simulated-tests.xml" \
  --junit "$verification/p610-golden-tests-r1.xml" \
  --junit "$verification/p610-golden-tests-r2.xml" \
  --required-repeats 3 \
  --output "$verification/p610-simulated-evaluation.json"

for repeat in 1 2 3; do
  uv run --locked python scripts/evaluate_p6_embeddings.py \
    --output "$verification/p610-embedding-evaluation-r${repeat}.json" \
    > "$verification/p610-embedding-evaluation-r${repeat}.log"
done

HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 \
ALL_PROXY=http://127.0.0.1:9 NO_PROXY='' no_proxy='' \
uv run --locked python - <<'PY' > "$verification/p610-proxy-bypass.json"
import json
import math

from platform_agent_runtime.embedding_client import embedding_client_from_environment
from platform_core.domain.embeddings import EmbeddingRequest

client = embedding_client_from_environment()
result = client.embed(
    EmbeddingRequest(client.model, ("代理绕过真实性验证",), client.dimensions)
)
print(
    json.dumps(
        {
            "status": "PASS",
            "model": result.model,
            "dimensions": result.dimensions,
            "vectors": len(result.vectors),
            "finite": all(math.isfinite(value) for value in result.vectors[0]),
            "gateway": result.gateway,
        },
        ensure_ascii=False,
    )
)
PY

uv run --locked pytest -q tests/integration/test_api.py --tb=short \
  --junitxml="$verification/p610-api-tests.xml"
rm -rf "$release/dist"
mkdir -p "$release/dist"
uv build --all-packages --no-build-isolation --out-dir "$release/dist" \
  > "$verification/p610-release-build.log" 2>&1
uv run --locked python scripts/verify_p6_runtime_distribution.py \
  | tee "$verification/p610-distribution.log"
uv run --locked python scripts/record_p610.py
