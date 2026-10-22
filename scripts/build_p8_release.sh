#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

version="$(python - <<'PY'
import tomllib
from pathlib import Path

print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])
PY
)"
releases="$(pwd)/.p8-runtime/releases"
current_release="$(python - <<'PY'
import json
from pathlib import Path

root = Path.cwd()
pointer = json.loads((root / "docs/verification/p82-current-release.json").read_text())
print((root / pointer["release_directory"]).resolve())
PY
)"
source_release="${P8_SOURCE_RELEASE_DIR:-$current_release}"
export P8_SOURCE_RELEASE_DIR="$source_release"
[[ -f "$source_release/release-lock.json" ]] || { echo "P8.1 source release is missing" >&2; exit 2; }
eval "$(python - <<'PY'
import json
import os
lock = json.load(open(os.environ['P8_SOURCE_RELEASE_DIR'] + '/release-lock.json'))
names = {
    'operator': 'V7_OPERATOR_IMAGE',
    'qgis': 'V7_SPATIAL_QGIS_IMAGE_ID',
    'spatial_native': 'V7_SPATIAL_NATIVE_IMAGE_ID',
    'checker': 'V7_CHECKER_IMAGE',
    'gdal_checker': 'V7_GDAL_CHECKER_IMAGE',
    'tabular': 'V7_TABULAR_OPERATOR_IMAGE',
}
for key, env in names.items():
    print(f"export {env}='{lock['images'][key]}'")
PY
)"
mkdir -p "$releases"
bash scripts/build_p4_image.sh
export V7_CWL_IMAGE_ID="$(<.p4-runtime/cwl-image.txt)"
release_dir="$(mktemp -d "$releases/.${version}-build-XXXXXXXX")"
trap 'rm -rf -- "$release_dir"' EXIT
mkdir -p "$release_dir/wheels" "$release_dir/dependencies"

uv build --all-packages --no-build-isolation --out-dir "$release_dir/wheels"
uv export --locked --no-dev --no-emit-workspace --format requirements-txt \
  --output-file "$release_dir/requirements.txt" >/dev/null
uvx --from pip pip download --only-binary=:all: --no-deps --require-hashes \
  --index-url https://pypi.org/simple -r "$release_dir/requirements.txt" \
  -d "$release_dir/dependencies"
uv run --locked python scripts/build_operator_catalog.py generate \
  --output "$release_dir/operator-catalog.json"
uv run --locked python scripts/generate_checker_catalog.py \
  --checker-image "$V7_CHECKER_IMAGE" \
  --gdal-checker-image "$V7_GDAL_CHECKER_IMAGE" \
  --output "$release_dir/checker-catalog.json"
uv run --locked agent-tool-catalog generate --stage p7-science \
  --output "$release_dir/tool-catalog.json"
cp deploy/p6/model-catalog.deepseek-flash.json "$release_dir/model-catalog.json"
cp deploy/p8/embedding-catalog.disabled.json "$release_dir/embedding-catalog.json"
uv run --locked python scripts/generate_agent_runtime_profile.py \
  --embedding-catalog "$release_dir/embedding-catalog.json" \
  --output "$release_dir/agent-runtime-profile.json"

source_digest="$(uv run --locked python scripts/release_source_manifest.py --digest-only)"
docker build \
  --build-arg "V7_VERSION=$version" \
  --build-arg "V7_RELEASE_SOURCE_DIGEST=$source_digest" \
  --tag "groundwater-v7-app:$version" \
  --file deploy/p8/app.Dockerfile \
  "$release_dir"
app_image="$(docker image inspect "groundwater-v7-app:$version" --format '{{.Id}}')"

export V7_APP_IMAGE="$app_image"
export V7_POSTGRES_IMAGE="sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
uv run --locked python scripts/build_p7_release_lock.py \
  --operator-catalog "$release_dir/operator-catalog.json" \
  --checker-catalog "$release_dir/checker-catalog.json" \
  --tool-catalog "$release_dir/tool-catalog.json" \
  --wheel-dir "$release_dir/wheels" \
  --dependency-dir "$release_dir/dependencies" \
  --model-catalog "$release_dir/model-catalog.json" \
  --runtime-profile "$release_dir/agent-runtime-profile.json" \
  --upgrade-from-lock "$source_release/release-lock.json" \
  --schema-version 3 \
  --app-image-ref "groundwater-v7-app:$version" \
  --version "$version" \
  --output "$release_dir/release-lock.json"
printf '%s\n' "$app_image" > "$release_dir/app-image.txt"
# mktemp creates the staging root with mode 0700.  Release files are mounted
# read-only into containers running as uid 65532, so every directory must be
# traversable and every artifact readable before the immutable rename.
find "$release_dir" -type d -exec chmod 0555 {} +
find "$release_dir" -type f -exec chmod 0444 {} +
release_digest="$(python - "$release_dir/release-lock.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))['release_digest'].split(':', 1)[1])
PY
)"
final="$releases/$version-$release_digest"
[[ ! -e "$final" ]] || { echo "Refusing to overwrite immutable release $final" >&2; exit 2; }
mv "$release_dir" "$final"
trap - EXIT
echo "Built immutable P8.2 release in $final"
