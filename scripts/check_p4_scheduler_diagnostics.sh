#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard)$ ]]; then
  echo "usage: bash scripts/check_p4_scheduler_diagnostics.sh --profile {quick|standard}" >&2
  exit 2
fi
profile="$2"
while IFS='=' read -r name value; do
  if [[ "$name" == V7_* ]]; then unset "$name"; fi
done < <(env)
uv sync --locked
bash scripts/build_p3_images.sh >/dev/null
bash scripts/build_p4_performance_image.sh >/dev/null
bash scripts/build_p4_image.sh >/dev/null
uv run --locked ruff check benchmarks/p4 tests/performance
uv run --locked ruff format --check benchmarks/p4 tests/performance
uv run --locked pytest -q tests/performance --tb=short
uv run --locked python benchmarks/p4/scheduler_diagnostics.py --profile "$profile"
