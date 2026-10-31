#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
mkdir -p ${V7_P4_RUNTIME_ROOT}/remote/image
find ${V7_P4_RUNTIME_ROOT}/remote/image -maxdepth 1 -name 'groundwater_operator_pack-*.whl' -delete
uv build --package groundwater-operator-pack --wheel --out-dir ${V7_P4_RUNTIME_ROOT}/remote/image
cp deploy/spatial/native/native.Dockerfile ${V7_P4_RUNTIME_ROOT}/remote/image/native.Dockerfile
docker build -f ${V7_P4_RUNTIME_ROOT}/remote/image/native.Dockerfile \
  -t groundwater-v7-spatial-native:0.6.2 ${V7_P4_RUNTIME_ROOT}/remote/image
docker image inspect groundwater-v7-spatial-native:0.6.2 --format '{{.Id}}' \
  > ${V7_P4_RUNTIME_ROOT}/remote/job-image.txt
cp deploy/spatial/remote/remote_peer.py ${V7_P4_RUNTIME_ROOT}/remote/image/remote_peer.py
