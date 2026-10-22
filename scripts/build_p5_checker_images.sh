#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
mkdir -p .p5-runtime/images
rm -f .p5-runtime/images/groundwater_checker_pack-*.whl
cp ".p3-runtime/images/wheels/groundwater_checker_pack-${version}-py3-none-any.whl" .p5-runtime/images/
cp deploy/p5/gdal-checker.Dockerfile .p5-runtime/images/
source_digest="$(uv run --locked python scripts/release_source_manifest.py --digest-only)"
docker build --build-arg "V7_RELEASE_SOURCE_DIGEST=$source_digest" --build-arg "V7_VERSION=$version" \
  -f .p5-runtime/images/gdal-checker.Dockerfile \
  -t "groundwater-v7-gdal-checker:$version" .p5-runtime/images
checker_image="$(docker image inspect "groundwater-v7-checker:$version" --format '{{.Id}}')"
gdal_image="$(docker image inspect "groundwater-v7-gdal-checker:$version" --format '{{.Id}}')"
uv run --locked python scripts/generate_checker_catalog.py \
  --checker-image "$checker_image" --gdal-checker-image "$gdal_image" \
  --output .p5-runtime/checker-catalog.json
printf 'CHECKER=%s\nGDAL_CHECKER=%s\n' "$checker_image" "$gdal_image"
