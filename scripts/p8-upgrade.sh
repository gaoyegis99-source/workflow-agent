#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

usage() {
  cat >&2 <<'EOF'
usage:
  bash scripts/p8-upgrade.sh plan SOURCE_ENV TARGET_RELEASE
  bash scripts/p8-upgrade.sh prepare SOURCE_ENV TARGET_RELEASE OPERATION_DIR
  bash scripts/p8-upgrade.sh apply OPERATION_DIR
  bash scripts/p8-upgrade.sh verify OPERATION_DIR
  bash scripts/p8-upgrade.sh commit OPERATION_DIR
  bash scripts/p8-upgrade.sh rollback OPERATION_DIR
EOF
  exit 2
}

command="${1:-}"
[[ -n "$command" ]] || usage

upgrade=(uv run --locked platform-release-upgrade)

operation_value() {
  local operation="$1" key="$2"
  python - "$operation/operation.json" "$key" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
result = value[sys.argv[2]]
if not isinstance(result, (str, int)):
    raise SystemExit("operation field is not scalar")
print(result)
PY
}

env_value() {
  local file="$1" key="$2"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$file"
}

compose_for() {
  local file="$1"
  printf '%s\0' docker compose --env-file "$file" -f deploy/p8.compose.yaml
}

services=(platform-worker authoring-worker data-worker data-document-worker data-spatial-worker execution-supervisor execution-node-1 execution-node-2 execution-publication knowledge-worker agent-worker gateway platform-api data-api execution-api knowledge-api)
workers=(platform-worker authoring-worker data-worker data-document-worker data-spatial-worker execution-supervisor execution-node-1 execution-node-2 execution-publication knowledge-worker agent-worker)
apis=(gateway platform-api data-api execution-api knowledge-api)

automatic_rollback() {
  local status=$?
  trap - ERR
  echo "P8.2 operation failed; restoring the source release before commit" >&2
  bash "$0" rollback "$operation" || echo "P8.2 automatic rollback also failed" >&2
  exit "$status"
}

case "$command" in
  plan)
    [[ $# -eq 3 ]] || usage
    "${upgrade[@]}" plan "$(realpath "$2")" "$(realpath "$3")"
    ;;
  prepare)
    [[ $# -eq 4 ]] || usage
    source_env="$(realpath "$2")"
    target_release="$(realpath "$3")"
    operation="$4"
    "${upgrade[@]}" init "$source_env" "$target_release" "$operation"
    operation="$(realpath "$operation")"
    trap automatic_rollback ERR
    target_env="$operation/target.env"
    operation_id="$(operation_value "$operation" operation_id)"
    control_root="$(env_value "$source_env" V7_P8_CONTROL_DIR)"
    park_path="$control_root/park-manifests/$operation_id.json"
    "${upgrade[@]}" target-env "$source_env" "$target_release" "$target_env" \
      --park-manifest "$park_path"
    mapfile -d '' -t compose < <(compose_for "$source_env")
    mapfile -d '' -t target_compose < <(compose_for "$target_env")
    "${compose[@]}" exec -T gateway /bin/sh -ec 'umask 077; : > "$V7_MAINTENANCE_FILE"'
    "${upgrade[@]}" transition "$operation" BARRIER_ACTIVE >/dev/null
    "${compose[@]}" stop agent-worker authoring-worker
    park_path="$(env_value "$target_env" V7_PARK_MANIFEST)"
    target_digest="$(operation_value "$operation" target_release_digest)"
    source_release="$(operation_value "$operation" source_release_dir)"
    source_digest="$(operation_value "$operation" source_release_digest)"
    source_overrides=(
      -e "V7_RELEASE_DIGEST=$source_digest"
      -e "V7_RELEASE_LOCK=$source_release/release-lock.json"
      -e "V7_OPERATOR_CATALOG=$source_release/operator-catalog.json"
      -e "V7_CHECKER_CATALOG=$source_release/checker-catalog.json"
      -e "V7_AGENT_TOOL_CATALOG=$source_release/tool-catalog.json"
      -v "$source_release:$source_release:ro"
    )
    "${target_compose[@]}" --profile ops run --rm --no-deps "${source_overrides[@]}" backup \
      platform-release-upgrade park "$target_digest" "$park_path" --timeout 240
    "${upgrade[@]}" transition "$operation" PARKED >/dev/null
    "${compose[@]}" stop "${workers[@]}" "${apis[@]}"
    backup_root="$(realpath -m "$(env_value "$source_env" V7_P8_BACKUP_DIR)")"
    backup_dir="$backup_root/upgrade-$(operation_value "$operation" operation_id)"
    printf '%s\n' "$backup_dir" > "$operation/backup.path"
    chmod 0600 "$operation/backup.path"
    "${target_compose[@]}" --profile ops run --rm --no-deps "${source_overrides[@]}" backup \
      platform-cold-backup backup "$backup_dir" --park-manifest "$park_path"
    "${target_compose[@]}" --profile ops run --rm --no-deps "${source_overrides[@]}" backup \
      platform-cold-backup verify "$backup_dir"
    "${upgrade[@]}" transition "$operation" BACKED_UP >/dev/null
    trap - ERR
    echo "P8.2 prepare complete: $operation"
    ;;
  apply)
    [[ $# -eq 2 ]] || usage
    operation="$(realpath "$2")"
    trap automatic_rollback ERR
    source_env="$(operation_value "$operation" source_env)"
    target_env="$operation/target.env"
    [[ "$(operation_value "$operation" state)" == BACKED_UP ]] || { echo "operation is not BACKED_UP" >&2; exit 2; }
    mapfile -d '' -t source_compose < <(compose_for "$source_env")
    mapfile -d '' -t target_compose < <(compose_for "$target_env")
    "${source_compose[@]}" stop "${services[@]}" 2>/dev/null || true
    "${target_compose[@]}" up -d postgres volume-init migrate
    "${target_compose[@]}" wait migrate
    "${target_compose[@]}" up -d --wait platform-api data-api execution-api knowledge-api gateway
    "${upgrade[@]}" transition "$operation" TARGET_INSTALLED >/dev/null
    trap - ERR
    echo "P8.2 target APIs installed; workers remain stopped"
    ;;
  verify)
    [[ $# -eq 2 ]] || usage
    operation="$(realpath "$2")"
    trap automatic_rollback ERR
    [[ "$(operation_value "$operation" state)" == TARGET_INSTALLED ]] || { echo "operation is not TARGET_INSTALLED" >&2; exit 2; }
    source_env="$(operation_value "$operation" source_env)"
    target_env="$operation/target.env"
    mapfile -d '' -t compose < <(compose_for "$target_env")
    park_path="$(env_value "$target_env" V7_PARK_MANIFEST)"
    source_digest="$(operation_value "$operation" source_release_digest)"
    # The park manifest is written by the root-only ops container with mode
    # 0600.  Verify it in the same constrained mount namespace instead of
    # weakening evidence permissions for the host user.
    "${compose[@]}" --profile ops run --rm --no-deps backup \
      platform-release-upgrade verify-park "$park_path" --source-digest "$source_digest" \
      >/dev/null
    "${compose[@]}" ps --status running platform-api data-api execution-api knowledge-api gateway >/dev/null
    for port in 8012; do
      : "$port"
    done
    "${compose[@]}" --profile ops run --rm --no-deps backup platform-cold-backup status >/dev/null
    # Verify that the installed target still declares the exact N-1 recovery release.
    "${upgrade[@]}" plan "$source_env" "$(operation_value "$operation" target_release_dir)" >/dev/null
    "${upgrade[@]}" transition "$operation" TARGET_VERIFIED >/dev/null
    trap - ERR
    echo "P8.2 target verification complete; maintenance barrier remains active"
    ;;
  commit)
    [[ $# -eq 2 ]] || usage
    operation="$(realpath "$2")"
    trap automatic_rollback ERR
    [[ "$(operation_value "$operation" state)" == TARGET_VERIFIED ]] || { echo "operation is not TARGET_VERIFIED" >&2; exit 2; }
    target_env="$operation/target.env"
    mapfile -d '' -t compose < <(compose_for "$target_env")
    "${compose[@]}" up -d --wait "${workers[@]}" "${apis[@]}"
    "${upgrade[@]}" transition "$operation" COMMITTED >/dev/null
    trap - ERR
    # COMMITTED is the irreversible point.  If opening the gateway fails now,
    # maintenance remains active and an operator resumes this same release;
    # automatic N-1 restore is deliberately no longer allowed.
    "${upgrade[@]}" install-committed-environment "$operation" >/dev/null
    "${compose[@]}" exec -T gateway /bin/sh -ec 'rm -f -- "$V7_MAINTENANCE_FILE"'
    echo "P8.2 upgrade committed"
    ;;
  rollback)
    [[ $# -eq 2 ]] || usage
    operation="$(realpath "$2")"
    state="$(operation_value "$operation" state)"
    [[ "$state" != COMMITTED ]] || { echo "automatic rollback is forbidden after COMMITTED" >&2; exit 2; }
    [[ "$state" != ROLLED_BACK ]] || { echo "operation is already rolled back" >&2; exit 2; }
    source_env="$(operation_value "$operation" source_env)"
    target_env="$operation/target.env"
    "${upgrade[@]}" transition "$operation" ROLLING_BACK >/dev/null
    mapfile -d '' -t source_compose < <(compose_for "$source_env")
    mapfile -d '' -t target_compose < <(compose_for "$target_env")
    "${target_compose[@]}" stop "${services[@]}" 2>/dev/null || true
    "${source_compose[@]}" up -d postgres volume-init migrate
    "${source_compose[@]}" wait migrate
    "${source_compose[@]}" --profile ops run --rm --no-deps --entrypoint /bin/sh backup \
      -ec 'umask 077; : > "$V7_MAINTENANCE_FILE"'
    if [[ -f "$operation/backup.path" ]]; then
      backup_dir="$(cat "$operation/backup.path")"
      source_release="$(operation_value "$operation" source_release_dir)"
      source_digest="$(operation_value "$operation" source_release_digest)"
      source_overrides=(
        -e "V7_RELEASE_DIGEST=$source_digest"
        -e "V7_RELEASE_LOCK=$source_release/release-lock.json"
        -e "V7_OPERATOR_CATALOG=$source_release/operator-catalog.json"
        -e "V7_CHECKER_CATALOG=$source_release/checker-catalog.json"
        -e "V7_AGENT_TOOL_CATALOG=$source_release/tool-catalog.json"
        -v "$source_release:$source_release:ro"
      )
      "${target_compose[@]}" --profile ops run --rm --no-deps "${source_overrides[@]}" backup \
        platform-cold-backup verify "$backup_dir"
      "${target_compose[@]}" --profile ops run --rm --no-deps "${source_overrides[@]}" backup \
        platform-cold-backup restore "$backup_dir" --confirm-restore
    fi
    "${source_compose[@]}" up -d --wait
    "${source_compose[@]}" exec -T gateway /bin/sh -ec 'rm -f -- "$V7_MAINTENANCE_FILE"'
    "${upgrade[@]}" transition "$operation" ROLLED_BACK >/dev/null
    echo "P8.2 rollback complete"
    ;;
  *) usage ;;
esac
