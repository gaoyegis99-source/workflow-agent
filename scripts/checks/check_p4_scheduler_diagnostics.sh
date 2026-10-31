#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
if [[ $# -ne 2 || "$1" != "--profile" || ! "$2" =~ ^(quick|standard)$ ]]; then
  echo "usage: bash scripts/checks/check_p4_scheduler_diagnostics.sh --profile {quick|standard}" >&2
  exit 2
fi
profile="$2"
uv sync --locked
bash scripts/build/build_p3_images.sh >/dev/null
bash scripts/build/build_p4_performance_image.sh >/dev/null
bash scripts/build/build_p4_image.sh >/dev/null
uv run --locked ruff check benchmarks/p4 tests/performance
uv run --locked ruff format --check benchmarks/p4 tests/performance
uv run --locked pytest -q tests/performance --tb=short
uv run --locked python benchmarks/p4/scheduler_diagnostics.py --profile "$profile"
