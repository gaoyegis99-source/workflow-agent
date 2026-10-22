#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

phase=""
if [[ "${1:-}" == "--phase" ]]; then
  phase="${2:-}"
fi
case "$phase" in
  upgrade)
    uv run --locked python scripts/check_project_hygiene.py
    uv run --locked python scripts/check_workspace_dependencies.py
    uv run --locked pytest -q tests/p82 tests/p81
    uv run --locked ruff check \
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
