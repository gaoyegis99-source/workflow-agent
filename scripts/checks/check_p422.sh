#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
mkdir -p ${V7_P4_RUNTIME_ROOT}/verification
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
bash scripts/build/build_p3_images.sh > ${V7_P4_RUNTIME_ROOT}/verification/p3-images-p422.log 2>&1
bash scripts/build/build_p5_checker_images.sh \
  > ${V7_P4_RUNTIME_ROOT}/verification/checker-images-p422.log 2>&1
bash scripts/build/build_p4_image.sh > ${V7_P4_RUNTIME_ROOT}/verification/cwl-image-p422.log 2>&1
bash scripts/build/build_p42_images.sh > ${V7_P4_RUNTIME_ROOT}/verification/spatial-images-p422.log 2>&1
if [[ -r /etc/os-release ]] && rg -q 'VERSION_ID="20.04"' /etc/os-release; then
  export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64
fi
uv run --locked playwright install chromium > ${V7_P4_RUNTIME_ROOT}/verification/browser-install-p422.log 2>&1
uv run --locked pytest -q --tb=short --junitxml=${V7_P4_RUNTIME_ROOT}/verification/tests-p422.xml
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python scripts/checks/verify_distribution.py \
  | tee ${V7_P4_RUNTIME_ROOT}/verification/distribution-p422.log
printf '%s\n' "PASS: cartography results are available under $V7_P4_RUNTIME_ROOT/verification"
