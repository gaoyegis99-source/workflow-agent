#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard)$ ]]; then
  echo "usage: bash scripts/check_p4_scheduler_r5.sh --profile {quick|standard}" >&2
  exit 2
fi
profile="$2"
while IFS='=' read -r name value; do
  if [[ "$name" == V7_* ]]; then unset "$name"; fi
done < <(env)
mkdir -p .p4-runtime/verification
uv sync --locked
bash scripts/build_p3_images.sh >.p4-runtime/verification/r5-p3-images.log 2>&1
bash scripts/build_p4_performance_image.sh >.p4-runtime/verification/r5-performance-image.log 2>&1
bash scripts/build_p4_image.sh >.p4-runtime/verification/r5-cwl-image.log 2>&1
uv run --locked ruff check \
  packages/contracts/src/platform_contracts/engine_runtime.py \
  benchmarks/p4/scheduler_r5.py tests/p4/test_engine_runtime_profile.py \
  tests/performance/test_scheduler_r5.py \
  tests/performance/profile_execution_service.py
uv run --locked ruff format --check \
  packages/contracts/src/platform_contracts/engine_runtime.py \
  benchmarks/p4/scheduler_r5.py tests/p4/test_engine_runtime_profile.py \
  tests/performance/test_scheduler_r5.py \
  tests/performance/profile_execution_service.py
uv run --locked pytest -q \
  tests/p4/test_engine_runtime_profile.py tests/performance/test_scheduler_r5.py --tb=short
if [[ "$profile" == "standard" ]]; then
  rm -f .p4-runtime/verification/r5-chaining-safety.xml
  uv run --locked pytest -q tests/p4/test_chaining_profiles_e2e.py --tb=short \
    --junitxml=.p4-runtime/verification/r5-chaining-safety.xml
fi
uv run --locked python benchmarks/p4/scheduler_r5.py --profile "$profile"
