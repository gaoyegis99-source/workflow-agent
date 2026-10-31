#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
mkdir -p ${V7_P4_RUNTIME_ROOT}
docker image inspect groundwater-v7-operator:0.6.2 >/dev/null
docker build \
  -f tests/performance/long_operator.Dockerfile \
  -t groundwater-v7-performance-operator:0.6.2 \
  .
docker image inspect \
  groundwater-v7-performance-operator:0.6.2 \
  --format '{{.Id}}' > ${V7_P4_RUNTIME_ROOT}/performance-operator-image.txt
