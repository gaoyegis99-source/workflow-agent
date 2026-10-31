#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard)$ ]]; then
  echo "usage: bash scripts/checks/check_p4_scheduler_r5.sh --profile {quick|standard}" >&2
  exit 2
fi
profile="$2"
mkdir -p ${V7_P4_RUNTIME_ROOT}/verification
uv sync --locked
bash scripts/build/build_p3_images.sh >${V7_P4_RUNTIME_ROOT}/verification/r5-p3-images.log 2>&1
bash scripts/build/build_p4_performance_image.sh >${V7_P4_RUNTIME_ROOT}/verification/r5-performance-image.log 2>&1
bash scripts/build/build_p4_image.sh >${V7_P4_RUNTIME_ROOT}/verification/r5-cwl-image.log 2>&1
uv run --locked ruff check \
  packages/contracts/src/platform_contracts/engine_runtime.py \
  benchmarks/p4/scheduler_r5.py tests/execution/workflows/test_engine_runtime_profile.py \
  tests/performance/test_scheduler_r5.py \
  tests/performance/profile_execution_service.py
uv run --locked ruff format --check \
  packages/contracts/src/platform_contracts/engine_runtime.py \
  benchmarks/p4/scheduler_r5.py tests/execution/workflows/test_engine_runtime_profile.py \
  tests/performance/test_scheduler_r5.py \
  tests/performance/profile_execution_service.py
uv run --locked pytest -q \
  tests/execution/workflows/test_engine_runtime_profile.py tests/performance/test_scheduler_r5.py --tb=short
if [[ "$profile" == "standard" ]]; then
  rm -f ${V7_P4_RUNTIME_ROOT}/verification/r5-chaining-safety.xml
  uv run --locked pytest -q tests/execution/workflows/test_chaining_profiles_e2e.py --tb=short \
    --junitxml=${V7_P4_RUNTIME_ROOT}/verification/r5-chaining-safety.xml
fi
uv run --locked python benchmarks/p4/scheduler_r5.py --profile "$profile"
