#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
mkdir -p ${V7_P5_RUNTIME_ROOT}/images
rm -f ${V7_P5_RUNTIME_ROOT}/images/groundwater_checker_pack-*.whl
p3_runtime="${V7_P3_RUNTIME_ROOT:-${V7_P3_RUNTIME_ROOT}}"
cp "$p3_runtime/images/wheels/groundwater_checker_pack-${version}-py3-none-any.whl" ${V7_P5_RUNTIME_ROOT}/images/
cp deploy/knowledge/gdal-checker.Dockerfile ${V7_P5_RUNTIME_ROOT}/images/
source_digest="$(uv run --locked python scripts/release/release_source_manifest.py --digest-only)"
docker build --build-arg "V7_RELEASE_SOURCE_DIGEST=$source_digest" --build-arg "V7_VERSION=$version" \
  -f ${V7_P5_RUNTIME_ROOT}/images/gdal-checker.Dockerfile \
  -t "groundwater-v7-gdal-checker:$version" ${V7_P5_RUNTIME_ROOT}/images
checker_image="$(docker image inspect "groundwater-v7-checker:$version" --format '{{.Id}}')"
gdal_image="$(docker image inspect "groundwater-v7-gdal-checker:$version" --format '{{.Id}}')"
uv run --locked python scripts/build/generate_checker_catalog.py \
  --checker-image "$checker_image" --gdal-checker-image "$gdal_image" \
  --output ${V7_P5_RUNTIME_ROOT}/checker-catalog.json
printf 'CHECKER=%s\nGDAL_CHECKER=%s\n' "$checker_image" "$gdal_image"
