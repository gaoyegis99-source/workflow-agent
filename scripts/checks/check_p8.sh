#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"

phase=""
if [[ "${1:-}" == "--phase" ]]; then
  phase="${2:-}"
fi
case "$phase" in
  upgrade)
    "$V7_PYTHON" scripts/maintenance/check_internal_layout.py
    "$V7_PYTHON" scripts/maintenance/check_project_hygiene.py
    "$V7_PYTHON" scripts/maintenance/check_workspace_dependencies.py
    "$V7_PYTHON" -m pytest -q tests/release/upgrade tests/release/restore
    "$V7_PYTHON" -m ruff check \
      packages/contracts/src/platform_contracts/release.py \
      packages/contracts/src/platform_contracts/service.py \
      packages/platform-infrastructure/src/platform_infrastructure/upgrade.py \
      packages/platform-infrastructure/src/platform_infrastructure/cold_backup.py \
      apps/execution-service/src/platform_execution/application/workflow_runner.py
    ;;
  *)
    echo "supported P8 phase: upgrade" >&2
    exit 2
    ;;
esac
