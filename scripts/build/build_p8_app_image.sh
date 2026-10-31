#!/usr/bin/env bash
set -euo pipefail

V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"

version="$("$V7_PYTHON" -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
image="${1:-groundwater-v7-app:$version}"
root="$V7_RUNTIME_WORK_ROOT/p8/app-image"

rm -rf -- "$root"
mkdir -p -- "$root/wheels" "$root/dependencies"

"$V7_PYTHON" scripts/build/build_studio.py --output "$root/studio"
uv build --all-packages --wheel --no-build-isolation --out-dir "$root/wheels"
uv export --locked --no-dev --no-emit-workspace --format requirements-txt \
  --output-file "$root/requirements.txt" >/dev/null
uvx --from pip pip download --only-binary=:all: --no-deps --require-hashes \
  --index-url https://pypi.org/simple -r "$root/requirements.txt" -d "$root/dependencies"

source_digest="$("$V7_PYTHON" scripts/release/release_source_manifest.py --digest-only)"
docker build \
  --build-arg "V7_RELEASE_SOURCE_DIGEST=$source_digest" \
  --build-arg "V7_VERSION=$version" \
  -f deploy/production/app.Dockerfile \
  -t "$image" \
  "$root"
docker image inspect "$image" --format '{{.Id}}' > "$root/image-id.txt"
cat "$root/image-id.txt"
