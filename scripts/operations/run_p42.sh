#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
: "${V7_EXECUTION_URL:?source the explicit V7 environment first}"
if [[ ! -s ${V7_P4_RUNTIME_ROOT}/spatial/images.json ]]; then
  echo "missing fixed spatial images; run bash scripts/build/build_p42_images.sh" >&2
  exit 2
fi
readarray -t spatial_images < <(uv run --locked python - <<'PY'
import json
import os

value = json.load(open(os.path.join(os.environ["V7_P4_RUNTIME_ROOT"], "spatial", "images.json")))["images"]
print(value["qgis"])
print(value["native"])
PY
)
export V7_SPATIAL_QGIS_IMAGE_ID="${spatial_images[0]}"
export V7_SPATIAL_NATIVE_IMAGE_ID="${spatial_images[1]}"
export V7_DATA_SPATIAL_WORK_ROOT="${V7_DATA_SPATIAL_WORK_ROOT:-${V7_P4_RUNTIME_ROOT}/spatial-imports}"
exec bash scripts/operations/run_p4.sh
