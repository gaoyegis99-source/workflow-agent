#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard)$ ]]; then
  echo "usage: bash scripts/checks/check_p4_scheduler_r43.sh --profile {quick|standard}" >&2
  exit 2
fi
profile="$2"
mkdir -p ${V7_P4_RUNTIME_ROOT}/verification
uv sync --locked
bash scripts/build/build_p3_images.sh >${V7_P4_RUNTIME_ROOT}/verification/r43-p3-images.log 2>&1
bash scripts/build/build_p4_performance_image.sh >${V7_P4_RUNTIME_ROOT}/verification/r43-performance-image.log 2>&1
bash scripts/build/build_p4_image.sh >${V7_P4_RUNTIME_ROOT}/verification/r43-cwl-image.log 2>&1
uv run --locked ruff check benchmarks/p4 tests/performance tests/execution/workflows/test_execution_plan.py tests/execution/workflows/test_engine_package.py tests/execution/workflows/test_process_lifecycle.py tests/execution/workflows/test_bridge_identity.py tests/execution/workflows/test_execution_node_query.py tests/execution/workflows/test_node_budget.py tests/execution/workflows/test_workflow_trace.py
uv run --locked ruff format --check benchmarks/p4 tests/performance tests/execution/workflows/test_execution_plan.py tests/execution/workflows/test_engine_package.py tests/execution/workflows/test_process_lifecycle.py tests/execution/workflows/test_bridge_identity.py tests/execution/workflows/test_execution_node_query.py tests/execution/workflows/test_node_budget.py tests/execution/workflows/test_workflow_trace.py
uv run --locked pytest -q tests/performance tests/execution/workflows/test_execution_plan.py tests/execution/workflows/test_engine_package.py tests/execution/workflows/test_process_lifecycle.py tests/execution/workflows/test_bridge_identity.py tests/execution/workflows/test_execution_node_query.py tests/execution/workflows/test_node_budget.py tests/execution/workflows/test_workflow_trace.py --tb=short
uv run --locked python benchmarks/p4/scheduler_r43.py --profile "$profile"
