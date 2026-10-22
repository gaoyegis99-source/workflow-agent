#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
export V7_VERSION="$version"
mkdir -p .p4-runtime/spatial/images
rm -f .p4-runtime/spatial/images/groundwater_operator_pack-*.whl
uv build --package groundwater-operator-pack --wheel --out-dir .p4-runtime/spatial/images
cp deploy/p42/qgis.Dockerfile deploy/p42/native.Dockerfile .p4-runtime/spatial/images/
docker build -f .p4-runtime/spatial/images/qgis.Dockerfile \
  --build-arg V7_VERSION="$version" \
  -t "groundwater-v7-spatial-qgis:$version" .p4-runtime/spatial/images
docker build -f .p4-runtime/spatial/images/native.Dockerfile \
  --build-arg V7_VERSION="$version" \
  -t "groundwater-v7-spatial-native:$version" .p4-runtime/spatial/images
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
Path(".p4-runtime/spatial/images.json").write_text(
    json.dumps({"images": images, "versions": versions}, indent=2) + "\n"
)
PY
