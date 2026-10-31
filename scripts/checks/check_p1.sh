#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
bash scripts/checks/check_p0.sh
UV_PROJECT_ENVIRONMENT="$V7_P1_TOOLCHAIN_ENVIRONMENT" uv sync --locked --project experiments/p1/toil
export V7_P1_TOIL_PYTHON="$V7_P1_TOOLCHAIN_ENVIRONMENT/bin/python"
"$V7_PYTHON" -m unittest discover -s experiments/p1 -p 'test_*.py' -v
"$V7_PYTHON" experiments/p1/run.py "$@"
