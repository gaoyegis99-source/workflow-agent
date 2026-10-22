#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

command="${1:-}"
env_file="${2:-}"
if [[ -z "$env_file" || ! -f "$env_file" ]]; then
  echo "usage: bash scripts/p8-test-stack.sh up|status|down TEST_ENV_FILE" >&2
  exit 2
fi

env_file="$(realpath "$env_file")"
instance="$(awk -F= '$1 == "V7_DEPLOYMENT_ID" {sub(/^[^=]*=/, ""); print; exit}' "$env_file")"
[[ "$instance" == p8-test-* ]] || {
  echo "refusing non-test P8 instance: V7_DEPLOYMENT_ID must start with p8-test-" >&2
  exit 2
}

compose=(
  docker compose
  --env-file "$env_file"
  -f deploy/p8.compose.yaml
  -f deploy/p8.test-labels.compose.yaml
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
