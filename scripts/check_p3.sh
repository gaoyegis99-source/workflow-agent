#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p .p3-runtime/verification
# Never inherit deployment credentials into acceptance tests.
while IFS='=' read -r name value; do
  if [[ "$name" == V7_* ]]; then unset "$name"; fi
done < <(env)
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
bash scripts/build_p3_images.sh > .p3-runtime/verification/images.log 2>&1
# Chromium's Linux build also works on this Focal development host; record actual execution.
if [[ -r /etc/os-release ]] && rg -q 'VERSION_ID="20.04"' /etc/os-release; then
  export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64
fi
uv run --locked playwright install chromium > .p3-runtime/verification/browser-install.log 2>&1
uv run --locked pytest -q --tb=short --junitxml=.p3-runtime/verification/tests.xml
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python scripts/verify_distribution.py | tee .p3-runtime/verification/distribution.log
current_version="$(uv run --locked python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
if [[ "$current_version" == "0.2.1" ]]; then
  uv run --locked python scripts/record_p3.py
else
  echo "PASS: P3 compatibility regression under release $current_version; historical 0.2.1 evidence retained"
fi
