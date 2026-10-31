#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh" --testing
if [[ "${1:-}" != "--phase" || $# -ne 2 || ! "${2:-}" =~ ^(knowledge|research|acceptance|identity|capabilities|reliability|all)$ ]]; then
  echo "usage: bash scripts/checks/check_p5.sh --phase knowledge|research|acceptance|identity|capabilities|reliability|all" >&2
  exit 2
fi
phase="$2"
verification=${V7_P5_RUNTIME_ROOT}/verification
release=${V7_P5_RUNTIME_ROOT}/release
mkdir -p "$verification" "$release"
export UV_LINK_MODE=copy

# certifi contains mutable runtime data. Reinstall it as a private copy so an
# unrelated hard-linked uv environment cannot corrupt this gate's CA bundle.
uv sync --locked --reinstall-package certifi --link-mode copy
uv run --locked python - <<'PY'
import ssl
import certifi

ssl.create_default_context(cafile=certifi.where())
print("PASS: isolated certifi CA bundle")
PY
uv run --locked ruff check .
uv run --locked ruff format --check .

rm -rf "$release/dist"
mkdir -p "$release/dist"
uv run --locked python scripts/release/release_source_manifest.py \
  --output "$release/source-manifest.json" > "$verification/source-manifest.log"
uv build --all-packages --no-build-isolation --out-dir "$release/dist" \
  > "$verification/release-build.log" 2>&1

bash scripts/build/build_p3_images.sh > "$verification/p3-images.log" 2>&1
bash scripts/build/build_p5_checker_images.sh > "$verification/p5-checker-images.log" 2>&1
bash scripts/build/build_p42_images.sh > "$verification/spatial-images.log" 2>&1
export V7_CHECKER_CATALOG="$V7_P5_RUNTIME_ROOT/checker-catalog.json"
export V7_CHECKER_IMAGE="$(uv run --locked python -c \
  'import json, os; print(json.load(open(os.path.join(os.environ["V7_P5_RUNTIME_ROOT"], "checker-catalog.json")))["images"]["CHECKER"])')"
export V7_GDAL_CHECKER_IMAGE="$(uv run --locked python -c \
  'import json, os; print(json.load(open(os.path.join(os.environ["V7_P5_RUNTIME_ROOT"], "checker-catalog.json")))["images"]["GDAL_CHECKER"])')"
if [[ "$phase" != capabilities && "$phase" != reliability ]]; then
  bash scripts/build/build_p4_image.sh > "$verification/cwl-image.log" 2>&1
  if [[ -r /etc/os-release ]] && rg -q 'VERSION_ID="20.04"' /etc/os-release; then
    export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64
  fi
  uv run --locked playwright install chromium > "$verification/browser-install.log" 2>&1
fi

case "$phase" in
  knowledge) focused=(tests/knowledge/core) ;;
  research) focused=(tests/knowledge/core tests/knowledge/research) ;;
  acceptance) focused=(tests/knowledge/acceptance) ;;
  identity) focused=(tests/knowledge/identity) ;;
  capabilities) focused=(tests/knowledge/checkers) ;;
  reliability) focused=(tests/knowledge/reliability) ;;
  all) focused=(tests/knowledge/core tests/knowledge/research tests/knowledge/acceptance tests/knowledge/identity tests/knowledge/checkers tests/knowledge/reliability) ;;
esac
uv run --locked pytest -q "${focused[@]}" --tb=short \
  --junitxml="$verification/p5-focused-tests.xml"
if [[ "$phase" != capabilities && "$phase" != reliability ]]; then
  uv run --locked pytest -q --tb=short --junitxml="$verification/tests.xml"
fi
uv run --locked pytest -q tests/knowledge/core/test_http_e2e.py --tb=short \
  --junitxml="$verification/response-loss.xml"
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v \
  2>&1 | tee "$verification/p1-rules.log"
uv run --locked python scripts/checks/verify_distribution.py \
  | tee "$verification/distribution.log"

if [[ "$phase" == acceptance || "$phase" == identity || "$phase" == capabilities || "$phase" == reliability || "$phase" == all ]]; then
  V7_PYTHON="$V7_PYTHON" uv run --locked python scripts/checks/check_p53_checker.py \
    | tee "$verification/p53-checker.log"
fi
if [[ "$phase" == capabilities || "$phase" == reliability || "$phase" == all ]]; then
  uv run --locked python scripts/checks/check_p5_r2_checkers.py \
    | tee "$verification/p5-r2-checkers.log"
fi
printf '%s\n' "PASS: P5 results are available under $verification"
