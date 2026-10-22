#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard)$ ]]; then
  echo "usage: bash scripts/check_p4_scheduler_r44_roles.sh --profile {quick|standard}" >&2
  exit 2
fi
profile="$2"
while IFS='=' read -r name value; do
  if [[ "$name" == V7_* ]]; then unset "$name"; fi
done < <(env)
mkdir -p .p4-runtime/verification
uv sync --locked
bash scripts/build_p3_images.sh >.p4-runtime/verification/r44-roles-p3-images.log 2>&1
bash scripts/build_p4_performance_image.sh >.p4-runtime/verification/r44-roles-performance-image.log 2>&1
bash scripts/build_p4_image.sh >.p4-runtime/verification/r44-roles-cwl-image.log 2>&1
uv run --locked ruff check benchmarks/p4/scheduler_r44.py benchmarks/p4/scheduler_r44_roles.py tests/p4/test_work_class.py
uv run --locked ruff format --check benchmarks/p4/scheduler_r44.py benchmarks/p4/scheduler_r44_roles.py tests/p4/test_work_class.py
uv run --locked pytest -q tests/p4/test_work_class.py --tb=short
uv run --locked python benchmarks/p4/scheduler_r44_roles.py --profile "$profile"
