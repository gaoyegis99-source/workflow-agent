#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
if [[ "${1:-}" != "--phase" || $# -ne 2 || ! "${2:-}" =~ ^(workflows|spatial|cartography|remote)$ ]]; then
  echo "usage: bash scripts/checks/check_p4.sh --phase workflows|spatial|cartography|remote" >&2
  exit 2
fi
if [[ "$2" == "remote" ]]; then
  exec bash scripts/checks/check_p43.sh --phase direct
fi
if [[ "$2" == "cartography" ]]; then
  exec bash scripts/checks/check_p422.sh
fi
if [[ "$2" == "spatial" ]]; then
  exec bash scripts/checks/check_p42.sh
fi
mkdir -p ${V7_P4_RUNTIME_ROOT}/verification
# Acceptance owns its databases, credentials, processes and containers.
uv sync --locked
uv sync --locked --project experiments/p1/toil
uv run --locked ruff check .
uv run --locked ruff format --check .
bash scripts/build/build_p3_images.sh > ${V7_P4_RUNTIME_ROOT}/verification/p3-images.log 2>&1
bash scripts/build/build_p5_checker_images.sh \
  > ${V7_P4_RUNTIME_ROOT}/verification/checker-images.log 2>&1
bash scripts/build/build_p4_image.sh > ${V7_P4_RUNTIME_ROOT}/verification/cwl-image.log 2>&1
if [[ -r /etc/os-release ]] && rg -q 'VERSION_ID="20.04"' /etc/os-release; then
  export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64
fi
uv run --locked playwright install chromium > ${V7_P4_RUNTIME_ROOT}/verification/browser-install.log 2>&1
uv run --locked pytest -q --tb=short --junitxml=${V7_P4_RUNTIME_ROOT}/verification/tests.xml
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python scripts/checks/verify_distribution.py \
  | tee ${V7_P4_RUNTIME_ROOT}/verification/distribution.log
printf '%s\n' "PASS: P4 results are available under $V7_P4_RUNTIME_ROOT/verification"
