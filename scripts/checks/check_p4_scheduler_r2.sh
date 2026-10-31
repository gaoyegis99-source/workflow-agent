#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
if [[ "${1:-}" != "--profile" || "${2:-}" != "quick" && "${2:-}" != "standard" || $# -ne 2 ]]; then
  echo "usage: bash scripts/checks/check_p4_scheduler_r2.sh --profile {quick|standard}" >&2
  exit 2
fi
mkdir -p ${V7_P4_RUNTIME_ROOT}/verification
bash scripts/build/build_p4_image.sh > ${V7_P4_RUNTIME_ROOT}/verification/r2-cwl-image.log 2>&1
uv run --locked python benchmarks/p4/scheduler_r2.py --profile "$2"
