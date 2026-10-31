#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard|max)$ ]]; then
  echo "usage: bash scripts/checks/check_p4_performance.sh --profile {quick|standard|max}" >&2
  exit 2
fi
profile="$2"
mkdir -p "$V7_P4_RUNTIME_ROOT/verification"
uv sync --locked
bash scripts/build/build_p3_images.sh >/dev/null
bash scripts/build/build_p4_performance_image.sh >/dev/null
bash scripts/build/build_p4_image.sh >/dev/null
uv run --locked ruff check benchmarks/p4 tests/performance tests/platform/services/harness.py
uv run --locked ruff format --check benchmarks/p4 tests/performance tests/platform/services/harness.py
uv run --locked pytest -q tests/performance --tb=short
uv run --locked python benchmarks/p4/workflow_benchmark.py \
  --profile "$profile" \
  --output "$V7_P4_RUNTIME_ROOT/verification/p4-workflow-performance-evidence.json"
