#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${V7_EXECUTION_URL:?source the explicit V7 environment first}"
if [[ ! -s .p4-runtime/cwl-image.txt ]]; then
  echo "missing immutable CWL image; run bash scripts/build_p4_image.sh" >&2
  exit 2
fi
export V7_CWL_IMAGE="$(<.p4-runtime/cwl-image.txt)"
export V7_EXECUTION_CALLBACK_URL="$V7_EXECUTION_URL"
export V7_EXECUTION_WORKER_ROLES="supervisor node node node node node node node node publication"
unset V7_EXECUTION_WORKERS || true
exec bash scripts/run_p3.sh
