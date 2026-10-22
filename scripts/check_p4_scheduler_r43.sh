#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard)$ ]]; then
  echo "usage: bash scripts/check_p4_scheduler_r43.sh --profile {quick|standard}" >&2
  exit 2
fi
profile="$2"
while IFS='=' read -r name value; do
  if [[ "$name" == V7_* ]]; then unset "$name"; fi
done < <(env)
mkdir -p .p4-runtime/verification
uv sync --locked
bash scripts/build_p3_images.sh >.p4-runtime/verification/r43-p3-images.log 2>&1
bash scripts/build_p4_performance_image.sh >.p4-runtime/verification/r43-performance-image.log 2>&1
bash scripts/build_p4_image.sh >.p4-runtime/verification/r43-cwl-image.log 2>&1
uv run --locked ruff check benchmarks/p4 tests/performance tests/p4/test_execution_plan.py tests/p4/test_engine_package.py tests/p4/test_process_lifecycle.py tests/p4/test_bridge_identity.py tests/p4/test_execution_node_query.py tests/p4/test_node_budget.py tests/p4/test_workflow_trace.py
uv run --locked ruff format --check benchmarks/p4 tests/performance tests/p4/test_execution_plan.py tests/p4/test_engine_package.py tests/p4/test_process_lifecycle.py tests/p4/test_bridge_identity.py tests/p4/test_execution_node_query.py tests/p4/test_node_budget.py tests/p4/test_workflow_trace.py
uv run --locked pytest -q tests/performance tests/p4/test_execution_plan.py tests/p4/test_engine_package.py tests/p4/test_process_lifecycle.py tests/p4/test_bridge_identity.py tests/p4/test_execution_node_query.py tests/p4/test_node_budget.py tests/p4/test_workflow_trace.py --tb=short
uv run --locked python benchmarks/p4/scheduler_r43.py --profile "$profile"
