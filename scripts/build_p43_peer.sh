#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .p4-runtime/remote/image
find .p4-runtime/remote/image -maxdepth 1 -name 'groundwater_operator_pack-*.whl' -delete
uv build --package groundwater-operator-pack --wheel --out-dir .p4-runtime/remote/image
cp deploy/p42/native.Dockerfile .p4-runtime/remote/image/native.Dockerfile
docker build -f .p4-runtime/remote/image/native.Dockerfile \
  -t groundwater-v7-spatial-native:0.6.2 .p4-runtime/remote/image
docker image inspect groundwater-v7-spatial-native:0.6.2 --format '{{.Id}}' \
  > .p4-runtime/remote/job-image.txt
cp deploy/p43/remote_peer.py .p4-runtime/remote/image/remote_peer.py
