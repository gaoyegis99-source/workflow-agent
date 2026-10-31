#!/usr/bin/env bash
set -euo pipefail
if (( $# != 0 )); then
  printf "%s\\n" "usage: check_p2.sh (full integration gate; no phase arguments)" >&2
  exit 2
fi
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
mkdir -p ${V7_P2_RUNTIME_ROOT}/verification
# Prevent inherited deployment settings from connecting baseline tests to live state.
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q --tb=short --junitxml=${V7_P2_RUNTIME_ROOT}/verification/tests.xml
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python scripts/checks/verify_distribution.py
V7_POSTGRES_PASSWORD=configuration-validation-only "$V7_PROJECT_ROOT/bin/compose" -f deploy/platform/p2.compose.yaml config --quiet
printf '%s\n' "PASS: P2 results are available under $V7_P2_RUNTIME_ROOT/verification"
