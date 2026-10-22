#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ "${1:-}" != "--profile" || "${2:-}" != "quick" && "${2:-}" != "standard" || $# -ne 2 ]]; then
  echo "usage: bash scripts/check_p4_scheduler_r2.sh --profile {quick|standard}" >&2
  exit 2
fi
mkdir -p .p4-runtime/verification
bash scripts/build_p4_image.sh > .p4-runtime/verification/r2-cwl-image.log 2>&1
uv run --locked python benchmarks/p4/scheduler_r2.py --profile "$2"
