#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
P3_RUNTIME="$V7_P3_RUNTIME_ROOT"
mkdir -p "$P3_RUNTIME/verification"
# Never inherit deployment credentials into acceptance tests.
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
bash scripts/build/build_p3_images.sh > "$P3_RUNTIME/verification/images.log" 2>&1
bash scripts/build/build_p5_checker_images.sh \
  > "$P3_RUNTIME/verification/checker-images.log" 2>&1
# Chromium's Linux build also works on this Focal development host; record actual execution.
if [[ -r /etc/os-release ]] && rg -q 'VERSION_ID="20.04"' /etc/os-release; then
  export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64
fi
uv run --locked playwright install chromium > "$P3_RUNTIME/verification/browser-install.log" 2>&1
uv run --locked pytest -q --tb=short --junitxml="$P3_RUNTIME/verification/tests.xml"
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python scripts/checks/verify_distribution.py | tee "$P3_RUNTIME/verification/distribution.log"
printf '%s\n' "PASS: P3 results are available under $P3_RUNTIME/verification"
