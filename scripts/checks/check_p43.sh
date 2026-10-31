#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
phase="${2:-direct}"
if [[ "${1:-}" != "--phase" || "$phase" != "direct" ]]; then
  echo "usage: bash scripts/checks/check_p43.sh --phase direct" >&2
  exit 2
fi
uv sync --locked
uv run ruff check packages apps tests/spatial/remote deploy/spatial/remote/remote_peer.py
uv run pytest -q tests/contracts tests/architecture tests/integration/test_api.py \
  tests/spatial/remote tests/spatial/reliability tests/platform/services/test_publication.py tests/platform/services/test_execution.py \
  tests/spatial/native/test_migration.py tests/spatial/native/test_contracts.py tests/spatial/cartography/test_contracts.py
python experiments/p1/gate.py
