#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p .p4-runtime/verification
while IFS='=' read -r name value; do
  if [[ "$name" == V7_* ]]; then unset "$name"; fi
done < <(env)
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
bash scripts/build_p3_images.sh > .p4-runtime/verification/p3-images-p422.log 2>&1
bash scripts/build_p4_image.sh > .p4-runtime/verification/cwl-image-p422.log 2>&1
bash scripts/build_p42_images.sh > .p4-runtime/verification/spatial-images-p422.log 2>&1
if [[ -r /etc/os-release ]] && rg -q 'VERSION_ID="20.04"' /etc/os-release; then
  export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64
fi
uv run --locked playwright install chromium > .p4-runtime/verification/browser-install-p422.log 2>&1
uv run --locked pytest -q --tb=short --junitxml=.p4-runtime/verification/tests-p422.xml
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python scripts/verify_distribution.py \
  | tee .p4-runtime/verification/distribution-p422.log
uv run --locked python scripts/record_p422.py
