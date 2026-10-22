#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

command="${1:-}"
env_file="${2:-.p8-runtime/instances/local/p8.env}"
if [[ -z "$command" || ! -f "$env_file" ]]; then
  echo "usage: bash scripts/p8.sh up|status|maintenance-on|backup|verify|restore|resume|down ENV_FILE [PATH]" >&2
  exit 2
fi
env_file="$(realpath "$env_file")"
compose=(docker compose --env-file "$env_file" -f deploy/p8.compose.yaml)
backup_command=("${compose[@]}" --profile ops run --rm --no-deps backup platform-cold-backup)
producer_services=(agent-worker authoring-worker)
settlement_services=(platform-worker data-worker data-document-worker data-spatial-worker execution-supervisor execution-node-1 execution-node-2 execution-publication knowledge-worker)
api_services=(gateway platform-api data-api execution-api knowledge-api)
services=("${producer_services[@]}" "${settlement_services[@]}" "${api_services[@]}")

env_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$env_file"
}

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
    "${compose[@]}" --profile ops run --rm backup platform-cold-backup wait --timeout 600
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
