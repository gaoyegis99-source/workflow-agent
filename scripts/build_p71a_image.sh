#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"

root=.p7-runtime/tabular
rm -rf "$root"
mkdir -p "$root/wheels"
uv build --package groundwater-tabular-operator-pack --wheel --out-dir "$root/wheels"
uv export --locked --package groundwater-tabular-operator-pack --no-dev --no-emit-workspace \
  --format requirements-txt --output-file "$root/requirements.txt" >/dev/null
uvx --from pip pip download --only-binary=:all: --no-deps --require-hashes \
  --index-url https://pypi.org/simple -r "$root/requirements.txt" -d "$root/wheels"
cp deploy/p7/tabular.Dockerfile "$root/"
source_digest="$(uv run --locked python scripts/release_source_manifest.py --digest-only)"
docker build --build-arg "V7_RELEASE_SOURCE_DIGEST=$source_digest" --build-arg "V7_VERSION=$version" \
  -f "$root/tabular.Dockerfile" -t "groundwater-v7-tabular:$version" "$root"
docker image inspect "groundwater-v7-tabular:$version" --format '{{.Id}}' > "$root/image-id.txt"
cat "$root/image-id.txt"
