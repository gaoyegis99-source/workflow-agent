#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"

command="${1:-}"
runtime_root="$V7_P8_RUNTIME_ROOT"
env_file="${2:-${runtime_root}/instances/local/compose.env}"
if [[ -z "$command" || ! -f "$env_file" ]]; then
  echo "usage: bin/p8 preflight|up|status|maintenance-on|backup|verify|restore|resume|down COMPOSE_ENV [PATH]" >&2
  exit 2
fi
[[ ! -L "$env_file" ]] || { echo "compose environment must not be a symlink" >&2; exit 2; }
env_file="$(realpath "$env_file")"
[[ "$env_file" != "$V7_PROJECT_ROOT"/* ]] && { echo "compose environment is outside V7" >&2; exit 2; }
compose=("$V7_PROJECT_ROOT/bin/compose" --env-file "$env_file" -f deploy/production/p8.compose.yaml)
backup_run_options=(--rm --no-deps)
if [[ "${V7_BACKUP_IGNORE_PARK_MANIFEST:-false}" == "true" ]]; then
  backup_run_options+=(-e V7_PARK_MANIFEST=)
fi
backup_command=("${compose[@]}" --profile ops run "${backup_run_options[@]}" backup platform-cold-backup)
producer_services=(agent-worker authoring-worker)
settlement_services=(platform-worker-package platform-worker-action platform-worker-acceptance platform-worker-knowledge data-worker data-document-worker data-spatial-worker execution-supervisor execution-node-1 execution-node-2 execution-publication knowledge-worker)
api_services=(gateway platform-api data-api execution-api knowledge-api)
services=("${producer_services[@]}" "${settlement_services[@]}" "${api_services[@]}")

env_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$env_file"
}

preflight() {
  "$V7_PYTHON" -B "$V7_SOURCE_ROOT/scripts/operations/p8_preflight.py" \
    --project-root "$V7_PROJECT_ROOT" --env-file "$env_file" >/dev/null
  "$V7_PROJECT_ROOT/bin/docker" version >/dev/null
}

preflight

backup_path() {
  local requested="$1"
  local root
  root="$(realpath -m "$(env_value V7_P8_BACKUP_DIR)")"
  requested="$(realpath -m "$requested")"
  if [[ "$requested" != "$root"/* ]]; then
    echo "backup paths must be beneath $root" >&2
    exit 2
  fi
  printf '%s\n' "$requested"
}

case "$command" in
  preflight)
    echo "P8_DIRECT_COMPOSE_PREFLIGHT_OK"
    ;;
  up)
    "${compose[@]}" up -d --wait
    ;;
  status)
    "${compose[@]}" ps
    "${backup_command[@]}" status
    ;;
  maintenance-on)
    "${compose[@]}" exec -T gateway /bin/sh -ec 'umask 077; : > "$V7_MAINTENANCE_FILE"'
    "${backup_command[@]}" wait --timeout 600
    ;;
  backup)
    destination="${3:?backup destination is required}"
    destination="$(backup_path "$destination")"
    "${compose[@]}" exec -T gateway /bin/sh -ec 'umask 077; : > "$V7_MAINTENANCE_FILE"'
    "${backup_command[@]}" wait --timeout 600
    "${compose[@]}" stop "${producer_services[@]}"
    "${backup_command[@]}" wait --timeout 600
    "${compose[@]}" stop "${settlement_services[@]}" "${api_services[@]}"
    "${backup_command[@]}" backup "$destination"
    echo "Backup complete; deployment remains stopped in maintenance mode. Run resume after verification."
    ;;
  verify)
    source="${3:?backup source is required}"
    source="$(backup_path "$source")"
    "${backup_command[@]}" verify "$source"
    ;;
  restore)
    source="${3:?backup source is required}"
    source="$(backup_path "$source")"
    "${compose[@]}" stop "${services[@]}" 2>/dev/null || true
    "${compose[@]}" up -d --wait postgres volume-init migrate
    "${compose[@]}" wait migrate
    "${compose[@]}" --profile ops run --rm --no-deps --entrypoint /bin/sh backup -ec 'umask 077; : > "$V7_MAINTENANCE_FILE"'
    "${backup_command[@]}" restore "$source" --confirm-restore
    echo "Restore complete; deployment remains stopped in maintenance mode."
    ;;
  resume)
    "${compose[@]}" up -d --wait
    "${compose[@]}" exec -T gateway /bin/sh -ec 'rm -f -- "$V7_MAINTENANCE_FILE"'
    ;;
  down)
    "${compose[@]}" down
    ;;
  *)
    echo "unknown P8 command: $command" >&2
    exit 2
    ;;
esac
