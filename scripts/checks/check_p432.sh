#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
profile="${2:-quick}"
if [[ "${1:-}" != "--profile" || "$profile" != "quick" && "$profile" != "standard" ]]; then
  echo "usage: bash scripts/checks/check_p432.sh --profile quick|standard" >&2
  exit 2
fi
if [[ "$profile" == "standard" ]]; then
  : "${V7_P43_REMOTE_URL:?standard requires V7_P43_REMOTE_URL}"
  : "${V7_P43_REMOTE_TOKEN_FILE:?standard requires V7_P43_REMOTE_TOKEN_FILE}"
  : "${V7_P43_REMOTE_CA_FILE:?standard requires V7_P43_REMOTE_CA_FILE}"
  : "${V7_P43_REMOTE_IMPLEMENTATION_DIGEST:?standard requires V7_P43_REMOTE_IMPLEMENTATION_DIGEST}"
  [[ "$V7_P43_REMOTE_URL" == https://* ]] || {
    echo "standard requires an HTTPS remote peer" >&2
    exit 2
  }
fi
mkdir -p ${V7_P4_RUNTIME_ROOT}/verification
uv sync --locked
uv run ruff check packages apps tests/spatial/mixed_workflows tests/spatial/remote/conftest.py
uv run pytest -q tests/spatial/mixed_workflows tests/spatial/remote/test_registry_and_recovery.py \
  --junitxml="${V7_P4_RUNTIME_ROOT}/verification/p432-${profile}.xml"
if [[ "$profile" == "standard" ]]; then
  uv run pytest -q tests/spatial/remote/test_remote_e2e.py tests/integration/test_api.py \
    --junitxml=${V7_P4_RUNTIME_ROOT}/verification/p432-standard-compat.xml
fi
