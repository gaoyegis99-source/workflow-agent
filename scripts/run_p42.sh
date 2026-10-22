#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${V7_EXECUTION_URL:?source the explicit V7 environment first}"
if [[ ! -s .p4-runtime/spatial/images.json ]]; then
  echo "missing fixed spatial images; run bash scripts/build_p42_images.sh" >&2
  exit 2
fi
readarray -t spatial_images < <(uv run --locked python - <<'PY'
import json
value = json.load(open(".p4-runtime/spatial/images.json"))["images"]
print(value["qgis"])
print(value["native"])
PY
)
export V7_SPATIAL_QGIS_IMAGE_ID="${spatial_images[0]}"
export V7_SPATIAL_NATIVE_IMAGE_ID="${spatial_images[1]}"
export V7_DATA_SPATIAL_WORK_ROOT="${V7_DATA_SPATIAL_WORK_ROOT:-.p4-runtime/spatial-imports}"
exec bash scripts/run_p4.sh
