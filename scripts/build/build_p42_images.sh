#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
export V7_VERSION="$version"
mkdir -p ${V7_P4_RUNTIME_ROOT}/spatial/images
rm -f ${V7_P4_RUNTIME_ROOT}/spatial/images/groundwater_operator_pack-*.whl
uv build --package groundwater-operator-pack --wheel --out-dir ${V7_P4_RUNTIME_ROOT}/spatial/images
cp deploy/spatial/native/qgis.Dockerfile deploy/spatial/native/native.Dockerfile ${V7_P4_RUNTIME_ROOT}/spatial/images/
docker build -f ${V7_P4_RUNTIME_ROOT}/spatial/images/qgis.Dockerfile \
  --build-arg V7_VERSION="$version" \
  -t "groundwater-v7-spatial-qgis:$version" ${V7_P4_RUNTIME_ROOT}/spatial/images
docker build -f ${V7_P4_RUNTIME_ROOT}/spatial/images/native.Dockerfile \
  --build-arg V7_VERSION="$version" \
  -t "groundwater-v7-spatial-native:$version" ${V7_P4_RUNTIME_ROOT}/spatial/images
uv run python - <<'PY'
import json
import os
import subprocess
from pathlib import Path

version = os.environ["V7_VERSION"]
images = {}
for key, tag in {
    "qgis": f"groundwater-v7-spatial-qgis:{version}",
    "native": f"groundwater-v7-spatial-native:{version}",
}.items():
    images[key] = subprocess.check_output(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True
    ).strip()
versions = subprocess.check_output(
    [
        "docker", "run", "--rm", "--entrypoint", "/bin/sh",
        f"groundwater-v7-spatial-qgis:{version}", "-c",
        "python3 -c 'from qgis.core import Qgis; print(Qgis.QGIS_VERSION)' && "
        "gdalinfo --version && dpkg-query -W -f='${Version}\\n' libproj25 && "
        "dpkg-query -W -f='${Version}' fonts-noto-cjk",
    ],
    text=True,
).splitlines()
Path(os.environ["V7_P4_RUNTIME_ROOT"], "spatial", "images.json").write_text(
    json.dumps({"images": images, "versions": versions}, indent=2) + "\n"
)
PY
