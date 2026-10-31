#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"

command="${1:-}"
env_file="${2:-}"
if [[ -z "$env_file" || ! -f "$env_file" ]]; then
  echo "usage: bash scripts/verification/p8-test-stack.sh up|status|down TEST_ENV_FILE" >&2
  exit 2
fi

env_file="$(realpath "$env_file")"
project_name="$(awk -F= '$1 == "V7_P8_PROJECT_NAME" {sub(/^[^=]*=/, ""); print; exit}' "$env_file")"
instance="${project_name#groundwater-v7-p8-}"
[[ "$instance" == p8-test-* ]] || {
  echo "refusing non-test P8 instance: V7_P8_PROJECT_NAME must identify p8-test-*" >&2
  exit 2
}
export V7_DEPLOYMENT_ID="$instance"

compose=(
  "$V7_PROJECT_ROOT/bin/compose"
  --env-file "$env_file"
  -f deploy/production/p8.compose.yaml
  -f deploy/production/p8.test-labels.compose.yaml
)

case "$command" in
  up)
    "${compose[@]}" up -d --wait
    ;;
  status)
    "${compose[@]}" ps
    ;;
  down)
    "${compose[@]}" down
    ;;
  *)
    echo "unknown P8 test-stack command: $command" >&2
    exit 2
    ;;
esac
