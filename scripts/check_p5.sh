#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ "${1:-}" != "--phase" || $# -ne 2 || ! "${2:-}" =~ ^(knowledge|research|acceptance|identity|capabilities|reliability|all)$ ]]; then
  echo "usage: bash scripts/check_p5.sh --phase knowledge|research|acceptance|identity|capabilities|reliability|all" >&2
  exit 2
fi
phase="$2"
verification=.p5-runtime/verification
release=.p5-runtime/release
mkdir -p "$verification" "$release"
export UV_LINK_MODE=copy
while IFS='=' read -r name value; do
  if [[ "$name" == V7_* ]]; then unset "$name"; fi
done < <(env)

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
uv run --locked python scripts/release_source_manifest.py \
  --output "$release/source-manifest.json" > "$verification/source-manifest.log"
uv build --all-packages --no-build-isolation --out-dir "$release/dist" \
  > "$verification/release-build.log" 2>&1

bash scripts/build_p3_images.sh > "$verification/p3-images.log" 2>&1
bash scripts/build_p5_checker_images.sh > "$verification/p5-checker-images.log" 2>&1
bash scripts/build_p42_images.sh > "$verification/spatial-images.log" 2>&1
export V7_CHECKER_CATALOG="$(pwd)/.p5-runtime/checker-catalog.json"
export V7_CHECKER_IMAGE="$(uv run --locked python -c \
  'import json; print(json.load(open(".p5-runtime/checker-catalog.json"))["images"]["CHECKER"])')"
export V7_GDAL_CHECKER_IMAGE="$(uv run --locked python -c \
  'import json; print(json.load(open(".p5-runtime/checker-catalog.json"))["images"]["GDAL_CHECKER"])')"
if [[ "$phase" != capabilities && "$phase" != reliability ]]; then
  bash scripts/build_p4_image.sh > "$verification/cwl-image.log" 2>&1
  if [[ -r /etc/os-release ]] && rg -q 'VERSION_ID="20.04"' /etc/os-release; then
    export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu22.04-x64
  fi
  uv run --locked playwright install chromium > "$verification/browser-install.log" 2>&1
fi

case "$phase" in
  knowledge) focused=(tests/p51) ;;
  research) focused=(tests/p51 tests/p52) ;;
  acceptance) focused=(tests/p53) ;;
  identity) focused=(tests/p5r1) ;;
  capabilities) focused=(tests/p5r2) ;;
  reliability) focused=(tests/p5r21) ;;
  all) focused=(tests/p51 tests/p52 tests/p53 tests/p5r1 tests/p5r2 tests/p5r21) ;;
esac
uv run --locked pytest -q "${focused[@]}" --tb=short \
  --junitxml="$verification/p5-focused-tests.xml"
if [[ "$phase" != capabilities && "$phase" != reliability ]]; then
  uv run --locked pytest -q --tb=short --junitxml="$verification/tests.xml"
fi
uv run --locked pytest -q tests/p51/test_http_e2e.py --tb=short \
  --junitxml="$verification/response-loss.xml"
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v \
  2>&1 | tee "$verification/p1-rules.log"
uv run --locked python scripts/verify_distribution.py \
  | tee "$verification/distribution.log"

if [[ "$phase" == acceptance || "$phase" == identity || "$phase" == capabilities || "$phase" == reliability || "$phase" == all ]]; then
  V7_PYTHON="$(pwd)/.venv/bin/python" uv run --locked python scripts/check_p53_checker.py \
    | tee "$verification/p53-checker.log"
fi
if [[ "$phase" == capabilities || "$phase" == reliability || "$phase" == all ]]; then
  uv run --locked python scripts/check_p5_r2_checkers.py \
    | tee "$verification/p5-r2-checkers.log"
fi
workspace_version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
if [[ "$phase" == all && "$workspace_version" == "0.7.2" ]]; then
  uv run --locked python scripts/record_p5_r21.py
fi
