#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ "${1:-}" != "--phase" || $# -ne 2 || ! "${2:-}" =~ ^(workflows|spatial|cartography|remote)$ ]]; then
  echo "usage: bash scripts/check_p4.sh --phase workflows|spatial|cartography|remote" >&2
  exit 2
fi
if [[ "$2" == "remote" ]]; then
  exec bash scripts/check_p43.sh --phase direct
fi
if [[ "$2" == "cartography" ]]; then
  exec bash scripts/check_p422.sh
fi
if [[ "$2" == "spatial" ]]; then
  exec bash scripts/check_p42.sh
fi
mkdir -p .p4-runtime/verification
# Acceptance owns its databases, credentials, processes and containers.
while IFS='=' read -r name value; do
  if [[ "$name" == V7_* ]]; then unset "$name"; fi
done < <(env)
uv sync --locked
uv sync --locked --project experiments/p1/toil
uv run --locked ruff check .
uv run --locked ruff format --check .
bash scripts/build_p3_images.sh > .p4-runtime/verification/p3-images.log 2>&1
bash scripts/build_p4_image.sh > .p4-runtime/verification/cwl-image.log 2>&1
if [[ -r /etc/os-release ]] && rg -q 'VERSION_ID="20.04"' /etc/os-release; then
  export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64
fi
uv run --locked playwright install chromium > .p4-runtime/verification/browser-install.log 2>&1
uv run --locked pytest -q --tb=short --junitxml=.p4-runtime/verification/tests.xml
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python scripts/verify_distribution.py \
  | tee .p4-runtime/verification/distribution.log
current_version="$(uv run --locked python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
if [[ "$current_version" == "0.3.8" ]]; then
  uv run --locked python scripts/record_p4.py
else
  echo "PASS: P4.1 compatibility regression under release $current_version; historical 0.3.8 evidence retained"
fi
