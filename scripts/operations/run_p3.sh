#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
: "${V7_DATABASE_URL:?source the explicit P3 environment first}"
: "${V7_DATA_DATABASE_URL:?required}"
: "${V7_EXECUTION_DATABASE_URL:?required}"
uv run --locked platform-admin check-schema
uv run --locked data-admin check-schema
uv run --locked execution-admin check-schema
pids=()
cleanup() { if (( ${#pids[@]} )); then kill "${pids[@]}" 2>/dev/null || true; wait "${pids[@]}" 2>/dev/null || true; fi; }
trap cleanup EXIT
trap 'exit 130' INT TERM
uv run --locked data-service & pids+=("$!")
uv run --locked execution-service & pids+=("$!")
uv run --locked platform-api --port "${V7_PLATFORM_PORT:-18070}" & pids+=("$!")
uv run --locked data-worker & pids+=("$!")
if [[ -n "${V7_SPATIAL_QGIS_IMAGE_ID:-}" ]]; then
  uv run --locked data-spatial-worker & pids+=("$!")
fi
if [[ -n "${V7_EXECUTION_WORKER_ROLES:-}" ]]; then
  read -r -a execution_roles <<<"$V7_EXECUTION_WORKER_ROLES"
  for role in "${execution_roles[@]}"; do
    if ! [[ "$role" =~ ^(supervisor|node|publication|all)$ ]]; then
      echo "V7_EXECUTION_WORKER_ROLES contains an invalid role" >&2
      exit 2
    fi
    uv run --locked execution-worker --role "$role" & pids+=("$!")
  done
else
  execution_workers="${V7_EXECUTION_WORKERS:-1}"
  if ! [[ "$execution_workers" =~ ^[1-9][0-9]*$ ]]; then
    echo "V7_EXECUTION_WORKERS must be a positive integer" >&2
    exit 2
  fi
  for ((worker = 0; worker < execution_workers; worker++)); do
    uv run --locked execution-worker & pids+=("$!")
  done
fi
for lane in package action acceptance knowledge; do
  uv run --locked platform-worker --lane "$lane" & pids+=("$!")
done
wait -n "${pids[@]}"
exit 1
