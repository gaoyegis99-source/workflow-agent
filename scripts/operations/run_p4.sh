#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
: "${V7_EXECUTION_URL:?source the explicit V7 environment first}"
if [[ ! -s ${V7_P4_RUNTIME_ROOT}/cwl-image.txt ]]; then
  echo "missing immutable CWL image; run bash scripts/build/build_p4_image.sh" >&2
  exit 2
fi
export V7_CWL_IMAGE="$(<${V7_P4_RUNTIME_ROOT}/cwl-image.txt)"
export V7_EXECUTION_CALLBACK_URL="$V7_EXECUTION_URL"
export V7_EXECUTION_WORKER_ROLES="supervisor node node node node node node node node publication"
unset V7_EXECUTION_WORKERS || true
exec bash scripts/operations/run_p3.sh
