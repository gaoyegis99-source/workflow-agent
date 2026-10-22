#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p .p4-runtime
docker image inspect groundwater-v7-operator:0.6.2 >/dev/null
docker build \
  -f tests/performance/long_operator.Dockerfile \
  -t groundwater-v7-performance-operator:0.6.2 \
  .
docker image inspect \
  groundwater-v7-performance-operator:0.6.2 \
  --format '{{.Id}}' > .p4-runtime/performance-operator-image.txt
