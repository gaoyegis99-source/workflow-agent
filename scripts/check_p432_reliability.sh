#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
profile="${2:-quick}"
if [[ "${1:-}" != "--profile" || "$profile" != "quick" && "$profile" != "standard" ]]; then
  echo "usage: bash scripts/check_p432_reliability.sh --profile quick|standard" >&2
  exit 2
fi
if [[ "$profile" == "standard" ]]; then
  : "${V7_P43_REMOTE_URL:?standard requires V7_P43_REMOTE_URL}"
  : "${V7_P43_REMOTE_TOKEN_FILE:?standard requires V7_P43_REMOTE_TOKEN_FILE}"
  : "${V7_P43_REMOTE_CA_FILE:?standard requires V7_P43_REMOTE_CA_FILE}"
  : "${V7_P43_REMOTE_IMPLEMENTATION_DIGEST:?standard requires implementation digest}"
  : "${V7_P43_REMOTE_RESTART_COMMAND_JSON:?standard requires a restricted SSH restart command}"
  [[ "$V7_P43_REMOTE_URL" == https://* ]] || {
    echo "standard requires an HTTPS remote peer" >&2
    exit 2
  }
fi
mkdir -p .p4-runtime/verification
uv sync --locked
uv run ruff check packages apps tests/p432 tests/p43 deploy/p43/remote_peer.py
uv run pytest -q \
  tests/p432/test_reliability_unit.py \
  tests/p432/test_mixed_workflow.py \
  tests/p432/test_mixed_faults.py \
  --junitxml=".p4-runtime/verification/p432-reliability-${profile}.xml"
