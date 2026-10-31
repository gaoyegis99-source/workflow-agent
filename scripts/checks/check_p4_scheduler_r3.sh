#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard)$ ]]; then
  echo "usage: bash scripts/checks/check_p4_scheduler_r3.sh --profile {quick|standard}" >&2
  exit 2
fi
profile="$2"
mkdir -p ${V7_P4_RUNTIME_ROOT}/verification
uv sync --locked
bash scripts/build/build_p3_images.sh >${V7_P4_RUNTIME_ROOT}/verification/r3-p3-images.log 2>&1
bash scripts/build/build_p4_performance_image.sh >${V7_P4_RUNTIME_ROOT}/verification/r3-performance-image.log 2>&1
bash scripts/build/build_p4_image.sh >${V7_P4_RUNTIME_ROOT}/verification/r3-cwl-image.log 2>&1
uv run --locked ruff check benchmarks/p4 tests/performance tests/execution/workflows/test_process_lifecycle.py
uv run --locked ruff format --check benchmarks/p4 tests/performance tests/execution/workflows/test_process_lifecycle.py
uv run --locked pytest -q tests/performance tests/execution/workflows/test_process_lifecycle.py --tb=short
uv run --locked python benchmarks/p4/scheduler_r3.py --profile "$profile"
