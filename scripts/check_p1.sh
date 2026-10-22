#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
bash scripts/check_p0.sh
uv sync --locked --project experiments/p1/toil
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python experiments/p1/run.py "$@"
