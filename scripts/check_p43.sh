#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
phase="${2:-direct}"
if [[ "${1:-}" != "--phase" || "$phase" != "direct" ]]; then
  echo "usage: bash scripts/check_p43.sh --phase direct" >&2
  exit 2
fi
uv sync --locked
uv run ruff check packages apps tests/p43 deploy/p43/remote_peer.py
uv run pytest -q tests/contracts tests/architecture tests/integration/test_api.py \
  tests/p43 tests/p431a tests/p3/test_publication.py tests/p3/test_execution.py \
  tests/p42/test_migration.py tests/p42/test_contracts.py tests/p422/test_contracts.py
python experiments/p1/gate.py
